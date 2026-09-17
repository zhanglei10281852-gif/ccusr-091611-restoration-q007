"""修复方案盲审与利益冲突管理。

用法概览：
    contract = load_contract()
    service = BlindReviewService(EventStore("events.jsonl"), contract, registry)
    service.register_case(...)       # 建立案件与截止日历
    service.submit_disclosure(...)   # 利益冲突申报（未完成者禁止接触方案）
    service.advance_phase(...)       # 按窗口推进阶段
    service.submit_opinion(...)      # 独立意见；逾期自动记为旁注
    service.cast_vote(...)           # 投票；同 vote_id 幂等
    service.seal_case(...)           # 封存计票快照
    service.unblind_case(...)        # 票数达标 + 秘书授权，不可逆
    service.publish_resolution(...)  # 决议：有效票数、异议链、确切版本
"""

from __future__ import annotations

import json
from pathlib import Path

from .calendar import CaseCalendar, Window, parse_dt
from .errors import (
    AccessDeniedError,
    AuthorizationError,
    BlindReviewError,
    CaseSealedError,
    CaseStateError,
    DuplicateVoteError,
    IdentityLeakError,
    IdentitySealError,
    QuorumNotMetError,
    WindowError,
)
from .identity import Identity, IdentityRegistry
from .notifications import NotificationEvent, Notifier
from .service import Actor, BlindReviewService
from .storage import EventStore

__all__ = [
    "AccessDeniedError",
    "Actor",
    "AuthorizationError",
    "BlindReviewError",
    "BlindReviewService",
    "CaseCalendar",
    "CaseSealedError",
    "CaseStateError",
    "DuplicateVoteError",
    "EventStore",
    "Identity",
    "IdentityLeakError",
    "IdentityRegistry",
    "IdentitySealError",
    "NotificationEvent",
    "Notifier",
    "QuorumNotMetError",
    "Window",
    "WindowError",
    "load_contract",
    "parse_dt",
]


def load_contract(path: str | Path | None = None) -> dict:
    """加载领域契约；默认取仓库内 domain/contract.json。"""
    if path is None:
        path = Path(__file__).resolve().parents[1] / "domain" / "contract.json"
    return json.loads(Path(path).read_text(encoding="utf-8"))
