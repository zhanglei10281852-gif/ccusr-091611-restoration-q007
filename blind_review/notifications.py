"""通知事件构造与身份安全检查。

所有提醒、质询路由、决议发布事件只携带案件引用与角色别名；
无论第几次重试，都不得包含真实身份字段或身份字符串。
"""

from __future__ import annotations

from .errors import IdentityLeakError


def build_reminder(kind: str, case_id: str, alias: str, window: dict, retry: int = 0) -> dict:
    return {
        "event": kind,
        "case_ref": case_id,
        "alias": alias,
        "retry": retry,
        "window": {"closes_at": window["closes_at"]},
    }


def routing_event(kind: str, case_id: str, alias: str, extra: dict | None = None) -> dict:
    event = {"event": kind, "case_ref": case_id, "alias": alias}
    if extra:
        event.update(extra)
    return event


def assert_identity_safe(event: dict, forbidden_fields, identity_strings=()) -> bool:
    """递归扫描事件载荷：出现受禁字段名或任何真实身份字符串即抛错。"""
    forbidden = set(forbidden_fields)
    needles = [s for s in identity_strings if s]

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in forbidden:
                    raise IdentityLeakError(f"通知包含受禁字段: {path}{key}")
                walk(value, f"{path}{key}.")
        elif isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                walk(value, f"{path}{index}.")
        elif isinstance(node, str):
            for needle in needles:
                if needle in node:
                    raise IdentityLeakError("通知内容泄露真实身份")

    walk(event, "")
    return True
