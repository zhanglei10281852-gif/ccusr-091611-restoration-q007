"""截止日历：按默认时长把四个窗口顺序排开。"""

from __future__ import annotations

from datetime import datetime, timedelta

STAGE_ORDER = [("disclosure", "disclosure_days"), ("review", "review_days"), ("questions", "questions_days"), ("voting", "voting_days")]


def build_windows(defaults: dict, opened_at: str) -> dict:
    """从案件开启时间生成 disclosure → review → questions → voting 的顺序窗口。"""
    cursor = datetime.fromisoformat(opened_at)
    windows = {}
    for name, days_key in STAGE_ORDER:
        closes = cursor + timedelta(days=defaults[days_key])
        windows[name] = {"opens_at": cursor.isoformat(), "closes_at": closes.isoformat()}
        cursor = closes
    return windows
