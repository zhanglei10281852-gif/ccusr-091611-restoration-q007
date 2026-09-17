"""盲审核心服务：阶段流转、申报门控、回避、重开、投票、封存、解盲与决议。

设计要点：
- 一切变更先写事件日志再应用，恢复时重放得到逐字节一致的封存结果；
- 所有内容只携带别名，真实身份经 IdentityRegistry 按敏感级别解析；
- 每个变更操作都检查案件阶段与截止窗口；封存/解盲后拒绝一切变更。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .calendar import PHASE_TO_WINDOW, STATE_ORDER, CaseCalendar
from .errors import (
    AccessDeniedError,
    AuthorizationError,
    CaseSealedError,
    CaseStateError,
    DuplicateVoteError,
    QuorumNotMetError,
    WindowError,
)
from .identity import IdentityRegistry
from .models import (
    Case,
    EvidenceAttachment,
    Inquiry,
    Opinion,
    Participant,
    ProposalSection,
    ProposalVersion,
    SealedRecord,
    Vote,
    canonical_hash,
)
from .notifications import Notifier
from .storage import EventStore

OPINION_RECOMMENDATIONS = {"approve", "approve_with_conditions", "reject"}
# 允许推进到的下一阶段（seal/unblind 是独立动作，不走 advance）
_ADVANCE_TARGETS = {"review_open", "questions_open", "voting"}


@dataclass(frozen=True)
class Actor:
    """操作者：角色 + 别名。角色用于授权，别名用于本人校验。"""

    role: str
    alias: str


class BlindReviewService:
    def __init__(
        self,
        store: EventStore,
        contract: dict,
        registry: IdentityRegistry | None = None,
        notifier: Notifier | None = None,
    ):
        self._store = store
        self._contract = contract
        self._registry = registry or IdentityRegistry()
        self.notifier = notifier or Notifier(
            registry=self._registry,
            allowed_event_types=set(contract.get("notification_events", [])),
            allowed_payload_keys=set(contract.get("notification_payload_allowed_keys", [])),
        )
        self._cases: dict[str, Case] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # 装配与恢复
    # ------------------------------------------------------------------ #

    @classmethod
    def load(
        cls,
        store_path: str | Path,
        contract: dict,
        registry: IdentityRegistry | None = None,
    ) -> "BlindReviewService":
        """从事件日志恢复：重放后封存记录与决议必须与崩溃前一致。"""
        service = cls(EventStore(store_path), contract, registry)
        for event in service._store.read_all():
            service._apply(event)
        return service

    def _emit(self, event_type: str, case_id: str, at: datetime, payload: dict) -> None:
        event = self._store.append(event_type, case_id, at.isoformat(), payload)
        self._apply(event)

    # ------------------------------------------------------------------ #
    # 案件建立与阶段流转
    # ------------------------------------------------------------------ #

    def register_case(
        self,
        *,
        actor: Actor,
        case_id: str,
        sensitivity: str,
        calendar: CaseCalendar,
        author_alias: str,
        reviewer_aliases: list[str],
        sections: list[dict],
        now: datetime,
        quorum: int | None = None,
        secretary_token: str,
    ) -> Case:
        with self._lock:
            self._require_role(actor, {"secretary"})
            if sensitivity not in self._contract["sensitivity_levels"]:
                raise ValueError(f"未知敏感级别: {sensitivity}")
            if case_id in self._cases:
                raise CaseStateError(f"案件已存在: {case_id}")
            participants = [{"alias": author_alias, "role": "proposal_author"}] + [
                {"alias": a, "role": "reviewer"} for a in reviewer_aliases
            ]
            self._emit(
                "case_registered",
                case_id,
                now,
                {
                    "sensitivity": sensitivity,
                    "quorum": quorum or self._contract.get("quorum_default", 3),
                    "secretary_token": secretary_token,
                    "author_alias": author_alias,
                    "calendar": calendar.to_dict(),
                    "participants": participants,
                    "sections": sections,
                },
            )
            return self._cases[case_id]

    def advance_phase(self, *, actor: Actor, case_id: str, now: datetime) -> str:
        """按顺序推进到下一阶段；下一阶段窗口必须已开始。"""
        with self._lock:
            self._require_role(actor, {"secretary"})
            case = self._case(case_id)
            idx = STATE_ORDER.index(case.state)
            target = STATE_ORDER[idx + 1]
            if target not in _ADVANCE_TARGETS:
                raise CaseStateError(f"{case.state} 之后须通过封存/解盲流转")
            window = case.calendar.window(PHASE_TO_WINDOW[target])
            if window.not_yet_open(now):
                raise WindowError(f"阶段 {target} 的窗口尚未开始")
            self._emit("phase_advanced", case_id, now, {"to_state": target})
            return target

    # ------------------------------------------------------------------ #
    # 利益冲突申报与回避
    # ------------------------------------------------------------------ #

    def submit_disclosure(
        self,
        *,
        actor: Actor,
        case_id: str,
        statement: str,
        has_conflict: bool,
        now: datetime,
    ) -> str:
        """申报利益关系。无冲突即 clear；有冲突进入 pending_decision 待裁定。"""
        with self._lock:
            case = self._case(case_id)
            self._require_mutable(case)
            participant = self._participant(case, actor.alias)
            window = case.calendar.window("disclosure")
            late = window.closed_by(now)
            self._emit(
                "disclosure_submitted",
                case_id,
                now,
                {
                    "alias": participant.alias,
                    "statement": statement,
                    "has_conflict": has_conflict,
                    "late": late,
                },
            )
            return case.participants[participant.alias].conflict_state

    def decide_disclosure(
        self,
        *,
        actor: Actor,
        case_id: str,
        alias: str,
        decision: str,
        reason: str,
        now: datetime,
    ) -> None:
        """主任委员/秘书裁定待决申报：clear 或 recused。"""
        with self._lock:
            self._require_role(actor, {"committee_chair", "secretary"})
            case = self._case(case_id)
            self._require_mutable(case)
            participant = self._participant(case, alias)
            if participant.conflict_state != "pending_decision":
                raise CaseStateError(f"{alias} 不在待裁定状态")
            if decision not in {"clear", "recused"}:
                raise ValueError(f"非法裁定: {decision}")
            self._emit(
                "disclosure_decided",
                case_id,
                now,
                {"alias": alias, "decision": decision, "reason": reason},
            )

    def recuse_reviewer(
        self, *, actor: Actor, case_id: str, alias: str, reason: str, now: datetime
    ) -> None:
        """回避专家：旧意见与旧投票保留但标记排除，不计入法定人数。"""
        with self._lock:
            self._require_role(actor, {"committee_chair", "secretary"})
            case = self._case(case_id)
            self._require_mutable(case)
            self._participant(case, alias)
            self._emit("reviewer_recused", case_id, now, {"alias": alias, "reason": reason})

    # ------------------------------------------------------------------ #
    # 方案版本与证据
    # ------------------------------------------------------------------ #

    def amend_proposal(
        self, *, actor: Actor, case_id: str, sections: list[dict], now: datetime
    ) -> dict:
        """修改方案：仅关键段落变化时，只重开覆盖这些段落的评审。"""
        with self._lock:
            case = self._case(case_id)
            self._require_author(case, actor)
            if case.state not in {"review_open", "questions_open"}:
                raise CaseStateError(f"{case.state} 阶段不允许修改方案")
            new_sections = [ProposalSection(**s) for s in sections]
            old_keys = case.current_version.key_section_map()
            new_keys = {s.section_id: s.content for s in new_sections if s.key}
            changed = sorted(
                sid for sid in set(old_keys) | set(new_keys) if old_keys.get(sid) != new_keys.get(sid)
            )
            reopened: list[dict] = []
            if changed:
                changed_set = set(changed)
                for opinion in sorted(case.opinions.values(), key=lambda o: o.opinion_id):
                    if opinion.state != "submitted":
                        continue
                    hit = sorted(set(opinion.sections) & changed_set)
                    if hit:
                        reopened.append({"opinion_id": opinion.opinion_id, "changed_sections": hit})
            self._emit(
                "proposal_amended",
                case_id,
                now,
                {
                    "version": case.current_version.version + 1,
                    "sections": [s.to_dict() for s in new_sections],
                    "changed_key_sections": changed,
                    "reopened": reopened,
                },
            )
            return {
                "version": case.current_version.version,
                "changed_key_sections": changed,
                "reopened_opinions": [r["opinion_id"] for r in reopened],
            }

    def attach_evidence(
        self, *, actor: Actor, case_id: str, attachment: dict, now: datetime
    ) -> None:
        with self._lock:
            case = self._case(case_id)
            self._require_author(case, actor)
            if case.state not in {"review_open", "questions_open"}:
                raise CaseStateError(f"{case.state} 阶段不允许补充证据")
            payload = dict(attachment)
            payload["attached_at"] = now.isoformat()
            payload["version"] = case.current_version.version
            self._emit("evidence_attached", case_id, now, payload)

    # ------------------------------------------------------------------ #
    # 独立意见与质询
    # ------------------------------------------------------------------ #

    def submit_opinion(
        self,
        *,
        actor: Actor,
        case_id: str,
        opinion_id: str,
        sections: list[str],
        recommendation: str,
        text: str,
        now: datetime,
    ) -> str:
        """提交独立意见。评审窗口关闭后提交的一律记为旁注（late_note），不计票。"""
        with self._lock:
            case = self._case(case_id)
            self._require_mutable(case)
            participant = self._participant(case, actor.alias)
            self._require_clear(participant)
            if recommendation not in OPINION_RECOMMENDATIONS:
                raise ValueError(f"非法意见结论: {recommendation}")

            existing = case.opinions.get(opinion_id)
            if existing is not None:
                # 被重开的草稿重新提交
                if existing.alias != actor.alias or existing.state != "draft":
                    raise CaseStateError(f"意见 {opinion_id} 不可重新提交")
                self._emit(
                    "opinion_submitted",
                    case_id,
                    now,
                    {
                        "opinion_id": opinion_id,
                        "alias": actor.alias,
                        "proposal_version": case.current_version.version,
                        "sections": list(sections),
                        "recommendation": recommendation,
                        "text": text,
                        "state": "submitted",
                    },
                )
                return "submitted"

            review_window = case.calendar.window("review")
            if case.state == "collecting_disclosures":
                raise CaseStateError("评审尚未开放，不能提交意见")
            on_time = case.state == "review_open" and review_window.contains(now)
            state = "submitted" if on_time else "late_note"
            self._emit(
                "opinion_submitted",
                case_id,
                now,
                {
                    "opinion_id": opinion_id,
                    "alias": actor.alias,
                    "proposal_version": case.current_version.version,
                    "sections": list(sections),
                    "recommendation": recommendation,
                    "text": text,
                    "state": state,
                },
            )
            return state

    def withdraw_opinion(self, *, actor: Actor, case_id: str, opinion_id: str, now: datetime) -> None:
        with self._lock:
            case = self._case(case_id)
            self._require_mutable(case)
            opinion = case.opinions.get(opinion_id)
            if opinion is None or opinion.alias != actor.alias:
                raise AccessDeniedError("只能撤回本人的意见")
            if opinion.state not in {"submitted", "late_note"}:
                raise CaseStateError(f"意见状态 {opinion.state} 不可撤回")
            self._emit("opinion_withdrawn", case_id, now, {"opinion_id": opinion_id})

    def raise_inquiry(self, *, actor: Actor, case_id: str, inquiry_id: str, text: str, now: datetime) -> None:
        with self._lock:
            case = self._case(case_id)
            self._require_phase(case, "questions_open")
            case.calendar.require_open("questions", now)
            participant = self._participant(case, actor.alias)
            self._require_clear(participant)
            self._emit(
                "inquiry_raised",
                case_id,
                now,
                {"inquiry_id": inquiry_id, "alias": actor.alias, "text": text},
            )

    def answer_inquiry(self, *, actor: Actor, case_id: str, inquiry_id: str, text: str, now: datetime) -> None:
        with self._lock:
            case = self._case(case_id)
            self._require_phase(case, "questions_open")
            case.calendar.require_open("questions", now)
            self._require_author(case, actor)
            if inquiry_id not in case.inquiries:
                raise CaseStateError(f"质询不存在: {inquiry_id}")
            self._emit("inquiry_answered", case_id, now, {"inquiry_id": inquiry_id, "text": text})

    # ------------------------------------------------------------------ #
    # 投票
    # ------------------------------------------------------------------ #

    def cast_vote(self, *, actor: Actor, case_id: str, vote_id: str, choice: str, now: datetime) -> Vote:
        """投票。同 vote_id 重试幂等；同一评审的新票取代旧票。"""
        with self._lock:
            case = self._case(case_id)
            self._require_phase(case, "voting")
            case.calendar.require_open("voting", now)
            participant = self._participant(case, actor.alias)
            self._require_clear(participant)
            if choice not in self._contract["vote_choices"]:
                raise ValueError(f"非法选项: {choice}")

            existing = case.votes.get(vote_id)
            if existing is not None:
                same = (
                    existing.reviewer_alias == actor.alias
                    and existing.choice == choice
                    and existing.proposal_version == case.current_version.version
                )
                if same:
                    return existing  # 幂等重试
                raise DuplicateVoteError(f"vote_id {vote_id} 已存在且载荷不同")

            supersedes = None
            for vote in case.votes.values():
                if (
                    vote.reviewer_alias == actor.alias
                    and not vote.withdrawn
                    and vote.superseded_by is None
                ):
                    supersedes = vote.vote_id
                    break
            self._emit(
                "vote_cast",
                case_id,
                now,
                {
                    "vote_id": vote_id,
                    "reviewer_alias": actor.alias,
                    "choice": choice,
                    "proposal_version": case.current_version.version,
                    "supersedes": supersedes,
                },
            )
            return case.votes[vote_id]

    def withdraw_vote(self, *, actor: Actor, case_id: str, vote_id: str, now: datetime) -> None:
        with self._lock:
            case = self._case(case_id)
            self._require_phase(case, "voting")
            case.calendar.require_open("voting", now)
            vote = case.votes.get(vote_id)
            if vote is None or vote.reviewer_alias != actor.alias:
                raise AccessDeniedError("只能撤回本人的投票")
            self._emit("vote_withdrawn", case_id, now, {"vote_id": vote_id})

    # ------------------------------------------------------------------ #
    # 封存、解盲与决议
    # ------------------------------------------------------------------ #

    def seal_case(self, *, actor: Actor, case_id: str, now: datetime) -> SealedRecord:
        """投票窗口关闭后封存：冻结有效票、排除项与法定人数结论。"""
        with self._lock:
            self._require_role(actor, {"secretary"})
            case = self._case(case_id)
            self._require_phase(case, "voting")
            if not case.calendar.window("voting").closed_by(now):
                raise WindowError("投票窗口尚未关闭，不能封存")

            adopted_version = case.current_version.version
            valid: list[dict] = []
            excluded: list[dict] = []
            for vote in sorted(case.votes.values(), key=lambda v: (v.cast_at, v.vote_id)):
                reason = self._vote_exclusion_reason(case, vote, adopted_version)
                if reason is None:
                    valid.append(vote.to_dict())
                else:
                    excluded.append(
                        {
                            "vote_id": vote.vote_id,
                            "reviewer_alias": vote.reviewer_alias,
                            "reason": reason,
                        }
                    )
            tally = {choice: 0 for choice in self._contract["vote_choices"]}
            for vote_dict in valid:
                tally[vote_dict["choice"]] += 1
            quorum_met = len(valid) >= case.quorum
            proposal_hash = case.current_version.content_hash()
            seal_hash = canonical_hash(
                {
                    "case_id": case_id,
                    "proposal_version": adopted_version,
                    "proposal_hash": proposal_hash,
                    "valid_vote_ids": sorted(v["vote_id"] for v in valid),
                    "tally": tally,
                    "quorum_required": case.quorum,
                }
            )
            record = SealedRecord(
                sealed_at=now.isoformat(),
                proposal_version=adopted_version,
                proposal_hash=proposal_hash,
                quorum_required=case.quorum,
                valid_votes=valid,
                excluded_votes=excluded,
                tally=tally,
                quorum_met=quorum_met,
                seal_hash=seal_hash,
            )
            self._emit("case_sealed", case_id, now, {"sealed_record": record.to_dict()})
            return case.sealed

    def unblind_case(self, *, actor: Actor, case_id: str, token: str, now: datetime) -> None:
        """解盲：票数达标 + 秘书授权，缺一不可；一旦解盲不可逆。"""
        with self._lock:
            self._require_role(actor, {"secretary"})
            case = self._case(case_id)
            if case.state == "unblinded":
                raise CaseSealedError("案件已解盲，不可逆")
            self._require_phase(case, "sealed")
            if token != case.secretary_token:
                raise AuthorizationError("秘书授权令牌不符")
            if not case.sealed.quorum_met:
                raise QuorumNotMetError(
                    f"有效票 {len(case.sealed.valid_votes)} 未达法定人数 {case.quorum}"
                )
            self._emit("case_unblinded", case_id, now, {"unblinded_by": actor.alias})

    def publish_resolution(self, *, actor: Actor, case_id: str, now: datetime) -> dict:
        """发布决议：有效票数、异议链与采用的确切方案版本，全部可核对。"""
        with self._lock:
            self._require_role(actor, {"secretary"})
            case = self._case(case_id)
            if case.state not in {"sealed", "unblinded"}:
                raise CaseStateError("未封存的案件不能发布决议")
            if case.resolution is not None:
                return case.resolution  # 幂等：重复发布返回同一份
            sealed = case.sealed
            dissent_chain = self._build_dissent_chain(case, sealed.proposal_version)
            resolution = {
                "resolution_id": f"{case_id}:resolution:v{sealed.proposal_version}",
                "case_id": case_id,
                "proposal_version": sealed.proposal_version,
                "proposal_hash": sealed.proposal_hash,
                "quorum": {
                    "required": sealed.quorum_required,
                    "valid_votes": len(sealed.valid_votes),
                    "met": sealed.quorum_met,
                },
                "tally": dict(sealed.tally),
                "valid_vote_ids": [v["vote_id"] for v in sealed.valid_votes],
                "excluded_votes": [dict(e) for e in sealed.excluded_votes],
                "dissent_chain": dissent_chain,
                "excluded_opinions": [
                    {"opinion_id": o.opinion_id, "alias": o.alias}
                    for o in sorted(case.opinions.values(), key=lambda x: x.opinion_id)
                    if o.state == "excluded"
                ],
                "seal_hash": sealed.seal_hash,
                "published_by": actor.alias,
                "published_at": now.isoformat(),
            }
            self._emit("resolution_published", case_id, now, {"resolution": resolution})
            return case.resolution

    def verify_resolution(self, *, case_id: str, resolution: dict) -> bool:
        """重算封存哈希与计票，并逐字段比对决议与封存记录是否一致。"""
        with self._lock:
            case = self._case(case_id)
            sealed = case.sealed
            if sealed is None:
                return False
            if resolution.get("proposal_version") != sealed.proposal_version:
                return False
            if resolution.get("valid_vote_ids") != [v["vote_id"] for v in sealed.valid_votes]:
                return False
            quorum = resolution.get("quorum", {})
            if quorum.get("required") != sealed.quorum_required:
                return False
            if quorum.get("valid_votes") != len(sealed.valid_votes):
                return False
            expected_hash = canonical_hash(
                {
                    "case_id": case_id,
                    "proposal_version": sealed.proposal_version,
                    "proposal_hash": sealed.proposal_hash,
                    "valid_vote_ids": sorted(v["vote_id"] for v in sealed.valid_votes),
                    "tally": sealed.tally,
                    "quorum_required": sealed.quorum_required,
                }
            )
            if resolution.get("seal_hash") != expected_hash:
                return False
            tally = {choice: 0 for choice in self._contract["vote_choices"]}
            for vote_dict in sealed.valid_votes:
                tally[vote_dict["choice"]] += 1
            if resolution.get("tally") != tally:
                return False
            adopted = case.versions[sealed.proposal_version - 1]
            return resolution.get("proposal_hash") == adopted.content_hash()

    # ------------------------------------------------------------------ #
    # 读取与身份解析
    # ------------------------------------------------------------------ #

    def get_proposal_content(self, *, actor: Actor, case_id: str) -> dict:
        """阅读方案内容：未完成冲突申报或已被回避者一律拒绝。"""
        with self._lock:
            case = self._case(case_id)
            if actor.role == "secretary" or actor.alias == case.author_alias:
                pass  # 秘书管理需要与作者本人不受申报门控限制
            else:
                participant = self._participant(case, actor.alias)
                self._require_clear(participant)
                if case.state == "collecting_disclosures":
                    raise CaseStateError("评审尚未开放")
            version = case.current_version
            return {
                "case_id": case_id,
                "proposal_version": version.version,
                "sections": [s.to_dict() for s in version.sections],
                "attachments": [a.to_dict() for a in version.attachments],
            }

    def list_opinions(self, *, actor: Actor, case_id: str) -> list[dict]:
        """解盲前评审只能看到自己的意见；作者解盲后才可见。"""
        with self._lock:
            case = self._case(case_id)
            unblinded = case.state == "unblinded"

            def view(o: Opinion) -> dict:
                return {
                    "opinion_id": o.opinion_id,
                    "alias": o.alias,
                    "proposal_version": o.proposal_version,
                    "recommendation": o.recommendation,
                    "state": o.state,
                    "submitted_at": o.submitted_at,
                }

            if actor.role == "secretary":
                return [view(o) for o in case.opinions.values()]
            participant = case.participants.get(actor.alias)
            if participant is None:
                raise AccessDeniedError("非本案参与者")
            if participant.role == "reviewer":
                if unblinded:
                    return [view(o) for o in case.opinions.values()]
                return [view(o) for o in case.opinions.values() if o.alias == actor.alias]
            if participant.role == "proposal_author" and unblinded:
                return [view(o) for o in case.opinions.values()]
            raise AccessDeniedError("当前阶段无权查看他人意见")

    def resolve_identity(self, *, actor: Actor, case_id: str, alias: str):
        """按敏感级别解析真实身份；越权或级别不足即拒绝。"""
        with self._lock:
            case = self._case(case_id)
            return self._registry.resolve(
                alias,
                sensitivity=case.sensitivity,
                unblinded=case.state == "unblinded",
                requester_role=actor.role,
            )

    def remind(self, *, case_id: str, event_type: str, recipient_alias: str, now: datetime):
        """发送提醒：只经 Notifier，载荷受契约白名单与泄露扫描约束。"""
        with self._lock:
            case = self._case(case_id)
            self._participant(case, recipient_alias)
            return self.notifier.send_reminder(
                case_id=case.case_id,
                event_type=event_type,
                recipient_alias=recipient_alias,
                now=now,
            )

    def get_case(self, case_id: str) -> Case:
        return self._case(case_id)

    # ------------------------------------------------------------------ #
    # 内部：校验
    # ------------------------------------------------------------------ #

    def _case(self, case_id: str) -> Case:
        try:
            return self._cases[case_id]
        except KeyError:
            raise CaseStateError(f"案件不存在: {case_id}") from None

    @staticmethod
    def _require_role(actor: Actor, roles: set[str]) -> None:
        if actor.role not in roles:
            raise AuthorizationError(f"角色 {actor.role} 无权执行该操作")

    @staticmethod
    def _require_mutable(case: Case) -> None:
        if case.state in {"sealed", "unblinded"}:
            raise CaseSealedError(f"案件已{case.state}，结果不可变更")

    @staticmethod
    def _require_phase(case: Case, state: str) -> None:
        if case.state == state:
            return
        if case.state in {"sealed", "unblinded"}:
            raise CaseSealedError(f"案件已{case.state}，结果不可变更")
        raise CaseStateError(f"当前阶段 {case.state}，需要 {state}")

    @staticmethod
    def _require_clear(participant: Participant) -> None:
        if participant.conflict_state != "clear":
            raise AccessDeniedError(
                f"{participant.alias} 冲突申报状态为 {participant.conflict_state}，禁止接触方案内容"
            )

    @staticmethod
    def _require_author(case: Case, actor: Actor) -> None:
        if actor.alias != case.author_alias:
            raise AuthorizationError("仅方案作者可执行该操作")

    @staticmethod
    def _participant(case: Case, alias: str) -> Participant:
        participant = case.participants.get(alias)
        if participant is None:
            raise AccessDeniedError(f"{alias} 不是本案参与者")
        return participant

    @staticmethod
    def _vote_exclusion_reason(case: Case, vote: Vote, adopted_version: int) -> str | None:
        if vote.withdrawn:
            return "withdrawn"
        if vote.superseded_by is not None:
            return "superseded"
        participant = case.participants.get(vote.reviewer_alias)
        if participant is None or participant.conflict_state != "clear":
            return "recused_or_undeclared"
        if vote.proposal_version != adopted_version:
            return "stale_version"
        return None

    def _build_dissent_chain(self, case: Case, adopted_version: int) -> list[dict]:
        """异议链：未通过的正式意见 + 迟到旁注 + 未答复质询，按时间排序。"""
        chain: list[dict] = []
        for opinion in case.opinions.values():
            if opinion.state == "submitted" and opinion.proposal_version == adopted_version:
                if opinion.recommendation != "approve":
                    chain.append(
                        {
                            "kind": "opinion",
                            "ref": opinion.opinion_id,
                            "alias": opinion.alias,
                            "recommendation": opinion.recommendation,
                            "text_hash": opinion.text_hash(),
                            "at": opinion.submitted_at,
                            "counted": True,
                        }
                    )
            elif opinion.state == "late_note":
                chain.append(
                    {
                        "kind": "late_note",
                        "ref": opinion.opinion_id,
                        "alias": opinion.alias,
                        "recommendation": opinion.recommendation,
                        "text_hash": opinion.text_hash(),
                        "at": opinion.submitted_at,
                        "counted": False,
                    }
                )
        for inquiry in case.inquiries.values():
            if not inquiry.answered:
                chain.append(
                    {
                        "kind": "inquiry",
                        "ref": inquiry.inquiry_id,
                        "alias": inquiry.alias,
                        "text_hash": canonical_hash({"inquiry_id": inquiry.inquiry_id, "text": inquiry.text}),
                        "at": inquiry.raised_at,
                        "counted": False,
                    }
                )
        chain.sort(key=lambda item: (item["at"], item["ref"]))
        return chain

    # ------------------------------------------------------------------ #
    # 内部：事件应用（恢复时重放同一代码路径）
    # ------------------------------------------------------------------ #

    def _apply(self, event: dict) -> None:
        handler = getattr(self, f"_on_{event['type']}", None)
        if handler is None:
            raise CaseStateError(f"未知事件类型: {event['type']}")
        handler(event)

    def _on_case_registered(self, event: dict) -> None:
        p = event["payload"]
        case = Case(
            case_id=event["case_id"],
            sensitivity=p["sensitivity"],
            state="collecting_disclosures",
            quorum=p["quorum"],
            secretary_token=p["secretary_token"],
            author_alias=p["author_alias"],
            calendar=CaseCalendar.from_dict(p["calendar"]),
        )
        for item in p["participants"]:
            case.participants[item["alias"]] = Participant(alias=item["alias"], role=item["role"])
        case.versions.append(
            ProposalVersion(version=1, sections=[ProposalSection(**s) for s in p["sections"]])
        )
        self._cases[case.case_id] = case

    def _on_phase_advanced(self, event: dict) -> None:
        self._cases[event["case_id"]].state = event["payload"]["to_state"]

    def _on_disclosure_submitted(self, event: dict) -> None:
        p = event["payload"]
        participant = self._cases[event["case_id"]].participants[p["alias"]]
        participant.disclosure_statement = p["statement"]
        participant.disclosure_at = event["at"]
        participant.conflict_state = "pending_decision" if p["has_conflict"] else "clear"

    def _on_disclosure_decided(self, event: dict) -> None:
        p = event["payload"]
        case = self._cases[event["case_id"]]
        case.participants[p["alias"]].conflict_state = p["decision"]
        if p["decision"] == "recused":
            case.participants[p["alias"]].recusal_reason = p["reason"]
            self._apply_recusal(case, p["alias"])

    def _on_reviewer_recused(self, event: dict) -> None:
        p = event["payload"]
        case = self._cases[event["case_id"]]
        participant = case.participants[p["alias"]]
        participant.conflict_state = "recused"
        participant.recusal_reason = p["reason"]
        self._apply_recusal(case, p["alias"])

    @staticmethod
    def _apply_recusal(case: Case, alias: str) -> None:
        """回避生效：旧意见保留但标记 excluded；未失效投票标记排除原因。"""
        for opinion in case.opinions.values():
            if opinion.alias == alias and opinion.state in {"draft", "submitted"}:
                opinion.state = "excluded"
        for vote in case.votes.values():
            if vote.reviewer_alias == alias and not vote.withdrawn and vote.superseded_by is None:
                vote.excluded_reason = "recused"

    def _on_proposal_amended(self, event: dict) -> None:
        p = event["payload"]
        case = self._cases[event["case_id"]]
        case.versions.append(
            ProposalVersion(
                version=p["version"], sections=[ProposalSection(**s) for s in p["sections"]]
            )
        )
        for item in p["reopened"]:
            opinion = case.opinions[item["opinion_id"]]
            opinion.state = "draft"
            opinion.reopen_history.append(
                {
                    "from_version": opinion.proposal_version,
                    "to_version": p["version"],
                    "changed_sections": item["changed_sections"],
                    "at": event["at"],
                }
            )

    def _on_evidence_attached(self, event: dict) -> None:
        p = event["payload"]
        case = self._cases[event["case_id"]]
        attachment = EvidenceAttachment(
            attachment_id=p["attachment_id"],
            title=p["title"],
            uri=p["uri"],
            sha256=p["sha256"],
            attached_at=p["attached_at"],
        )
        case.versions[p["version"] - 1].attachments.append(attachment)

    def _on_opinion_submitted(self, event: dict) -> None:
        p = event["payload"]
        case = self._cases[event["case_id"]]
        existing = case.opinions.get(p["opinion_id"])
        if existing is not None and existing.state == "draft":
            existing.proposal_version = p["proposal_version"]
            existing.sections = list(p["sections"])
            existing.recommendation = p["recommendation"]
            existing.text = p["text"]
            existing.state = "submitted"
            existing.submitted_at = event["at"]
        else:
            case.opinions[p["opinion_id"]] = Opinion(
                opinion_id=p["opinion_id"],
                alias=p["alias"],
                proposal_version=p["proposal_version"],
                sections=list(p["sections"]),
                recommendation=p["recommendation"],
                text=p["text"],
                state=p["state"],
                submitted_at=event["at"],
            )

    def _on_opinion_withdrawn(self, event: dict) -> None:
        case = self._cases[event["case_id"]]
        case.opinions[event["payload"]["opinion_id"]].state = "withdrawn"

    def _on_inquiry_raised(self, event: dict) -> None:
        p = event["payload"]
        case = self._cases[event["case_id"]]
        case.inquiries[p["inquiry_id"]] = Inquiry(
            inquiry_id=p["inquiry_id"], alias=p["alias"], text=p["text"], raised_at=event["at"]
        )

    def _on_inquiry_answered(self, event: dict) -> None:
        p = event["payload"]
        inquiry = self._cases[event["case_id"]].inquiries[p["inquiry_id"]]
        inquiry.answer_text = p["text"]
        inquiry.answered_at = event["at"]

    def _on_vote_cast(self, event: dict) -> None:
        p = event["payload"]
        case = self._cases[event["case_id"]]
        if p["supersedes"]:
            case.votes[p["supersedes"]].superseded_by = p["vote_id"]
        case.votes[p["vote_id"]] = Vote(
            vote_id=p["vote_id"],
            case_id=event["case_id"],
            proposal_version=p["proposal_version"],
            reviewer_alias=p["reviewer_alias"],
            choice=p["choice"],
            cast_at=event["at"],
        )

    def _on_vote_withdrawn(self, event: dict) -> None:
        case = self._cases[event["case_id"]]
        case.votes[event["payload"]["vote_id"]].withdrawn = True

    def _on_case_sealed(self, event: dict) -> None:
        case = self._cases[event["case_id"]]
        case.sealed = SealedRecord(**event["payload"]["sealed_record"])
        case.state = "sealed"

    def _on_case_unblinded(self, event: dict) -> None:
        case = self._cases[event["case_id"]]
        case.state = "unblinded"
        case.unblinded_at = event["at"]

    def _on_resolution_published(self, event: dict) -> None:
        case = self._cases[event["case_id"]]
        case.resolution = event["payload"]["resolution"]
