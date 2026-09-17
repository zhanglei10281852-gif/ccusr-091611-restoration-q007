"""领域模型：方案版本、证据、申报、意见、质询、投票、封存记录与决议。

所有模型只携带别名，不出现真实身份；内容哈希用于决议核对。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def canonical_hash(obj: Any) -> str:
    """对结构做规范化 JSON 后的 SHA-256，用于封存与核对。"""
    blob = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class ProposalSection:
    section_id: str
    title: str
    content: str
    key: bool = False  # 关键段落：修改后只重开覆盖这些段落的评审

    def to_dict(self) -> dict:
        return {
            "section_id": self.section_id,
            "title": self.title,
            "content": self.content,
            "key": self.key,
        }


@dataclass
class EvidenceAttachment:
    attachment_id: str
    title: str
    uri: str
    sha256: str
    attached_at: str

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class ProposalVersion:
    version: int
    sections: list[ProposalSection]
    attachments: list[EvidenceAttachment] = field(default_factory=list)

    def content_hash(self) -> str:
        return canonical_hash(
            {
                "sections": [s.to_dict() for s in self.sections],
                "attachments": [a.to_dict() for a in self.attachments],
            }
        )

    def key_section_map(self) -> dict[str, str]:
        return {s.section_id: s.content for s in self.sections if s.key}


@dataclass
class Participant:
    alias: str
    role: str  # reviewer / proposal_author / conservation_scientist ...
    conflict_state: str = "undeclared"  # undeclared|clear|pending_decision|recused
    disclosure_statement: str | None = None
    disclosure_at: str | None = None
    recusal_reason: str | None = None


@dataclass
class Opinion:
    opinion_id: str
    alias: str
    proposal_version: int
    sections: list[str]
    recommendation: str  # approve / approve_with_conditions / reject
    text: str
    state: str = "submitted"  # draft|submitted|withdrawn|excluded|late_note
    submitted_at: str = ""
    reopen_history: list[dict] = field(default_factory=list)

    def text_hash(self) -> str:
        return canonical_hash({"opinion_id": self.opinion_id, "text": self.text})


@dataclass
class Inquiry:
    inquiry_id: str
    alias: str
    text: str
    raised_at: str
    answer_text: str | None = None
    answered_at: str | None = None

    @property
    def answered(self) -> bool:
        return self.answer_text is not None


@dataclass
class Vote:
    vote_id: str
    case_id: str
    proposal_version: int
    reviewer_alias: str
    choice: str
    cast_at: str
    withdrawn: bool = False
    superseded_by: str | None = None
    excluded_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "vote_id": self.vote_id,
            "case_id": self.case_id,
            "proposal_version": self.proposal_version,
            "reviewer_alias": self.reviewer_alias,
            "choice": self.choice,
            "cast_at": self.cast_at,
        }


@dataclass
class SealedRecord:
    """封存时冻结的计票快照；恢复后必须逐字节一致。"""

    sealed_at: str
    proposal_version: int
    proposal_hash: str
    quorum_required: int
    valid_votes: list[dict]
    excluded_votes: list[dict]  # [{vote_id, reviewer_alias, reason}]
    tally: dict[str, int]
    quorum_met: bool
    seal_hash: str

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Case:
    case_id: str
    sensitivity: str
    state: str
    quorum: int
    secretary_token: str
    author_alias: str
    calendar: Any  # CaseCalendar，避免循环导入用 Any
    participants: dict[str, Participant] = field(default_factory=dict)
    versions: list[ProposalVersion] = field(default_factory=list)
    opinions: dict[str, Opinion] = field(default_factory=dict)
    inquiries: dict[str, Inquiry] = field(default_factory=dict)
    votes: dict[str, Vote] = field(default_factory=dict)
    sealed: SealedRecord | None = None
    unblinded_at: str | None = None
    resolution: dict | None = None

    @property
    def current_version(self) -> ProposalVersion:
        return self.versions[-1]

    def reviewer_aliases(self) -> list[str]:
        return [a for a, p in self.participants.items() if p.role == "reviewer"]
