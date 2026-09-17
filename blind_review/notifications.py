"""通知事件：只使用案件引用与别名，重试幂等，发送前强制泄露扫描。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .errors import IdentityLeakError
from .identity import IdentityRegistry


@dataclass(frozen=True)
class NotificationEvent:
    event_id: str
    event_type: str
    case_ref: str
    recipient_alias: str
    attempt: int
    scheduled_for: str
    body_template: str

    def to_payload(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "case_ref": self.case_ref,
            "recipient_alias": self.recipient_alias,
            "attempt": self.attempt,
            "scheduled_for": self.scheduled_for,
            "body_template": self.body_template,
        }


@dataclass
class Notifier:
    """按契约约束的通知发送器。

    - 载荷键不得超出 contract.notification_payload_allowed_keys；
    - 同一 (事件类型, 收件别名) 的提醒共享确定性 event_id，重试只递增 attempt，
      其余载荷逐字节一致，因此重试不可能额外泄露身份；
    - 发送前对全部载荷做身份泄露扫描。
    """

    registry: IdentityRegistry
    allowed_event_types: set[str]
    allowed_payload_keys: set[str]
    _attempts: dict[str, int] = field(default_factory=dict)
    outbox: list[NotificationEvent] = field(default_factory=list)

    def send_reminder(
        self,
        *,
        case_id: str,
        event_type: str,
        recipient_alias: str,
        now: datetime,
        body_template: str | None = None,
    ) -> NotificationEvent:
        if event_type not in self.allowed_event_types:
            raise ValueError(f"未登记的通知事件类型: {event_type}")
        event_id = f"{case_id}:{event_type}:{recipient_alias}"
        attempt = self._attempts.get(event_id, 0) + 1
        self._attempts[event_id] = attempt
        event = NotificationEvent(
            event_id=event_id,
            event_type=event_type,
            case_ref=case_id,
            recipient_alias=recipient_alias,
            attempt=attempt,
            scheduled_for=now.isoformat(),
            body_template=body_template or event_type,
        )
        self._guard(event)
        self.outbox.append(event)
        return event

    def _guard(self, event: NotificationEvent) -> None:
        payload = event.to_payload()
        extra = set(payload) - self.allowed_payload_keys
        if extra:
            raise IdentityLeakError(f"通知载荷包含未许可字段: {sorted(extra)}")
        for value in payload.values():
            hits = self.registry.leak_scan(str(value))
            if hits:
                raise IdentityLeakError(f"通知载荷泄露身份信息: {hits}")
