"""截止日历：各阶段窗口的解析与判定。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .errors import WindowError

# 案件阶段 → 日历窗口名
PHASE_TO_WINDOW = {
    "collecting_disclosures": "disclosure",
    "review_open": "review",
    "questions_open": "questions",
    "voting": "voting",
}

STATE_ORDER = [
    "collecting_disclosures",
    "review_open",
    "questions_open",
    "voting",
    "sealed",
    "unblinded",
]


def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"时间必须带时区: {value}")
    return dt


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    def contains(self, now: datetime) -> bool:
        return self.start <= now < self.end

    def closed_by(self, now: datetime) -> bool:
        return now >= self.end

    def not_yet_open(self, now: datetime) -> bool:
        return now < self.start


@dataclass(frozen=True)
class CaseCalendar:
    """一个案件的截止日历，窗口名见 contract.phase_windows。"""

    windows: dict[str, Window]

    @classmethod
    def from_dict(cls, data: dict[str, dict[str, str]]) -> "CaseCalendar":
        return cls(
            windows={
                name: Window(start=parse_dt(span["start"]), end=parse_dt(span["end"]))
                for name, span in data.items()
            }
        )

    def to_dict(self) -> dict[str, dict[str, str]]:
        return {
            name: {"start": w.start.isoformat(), "end": w.end.isoformat()}
            for name, w in self.windows.items()
        }

    def window(self, name: str) -> Window:
        try:
            return self.windows[name]
        except KeyError:
            raise WindowError(f"日历缺少窗口: {name}") from None

    def require_open(self, name: str, now: datetime) -> None:
        window = self.window(name)
        if window.not_yet_open(now):
            raise WindowError(f"窗口 {name} 尚未开放")
        if window.closed_by(now):
            raise WindowError(f"窗口 {name} 已截止")
