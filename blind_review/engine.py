"""修复方案盲审引擎。

职责：
- 按窗口流转方案版本、证据附件、冲突申报、独立意见、质询答复与决议；
- 未完成冲突申报者不能接触方案内容；
- 被回避专家的旧意见保留为 excluded，不计入法定人数与异议链；
- 关键段落修改只重开引用受影响段落的评审，其余意见随版本结转；
- 封存后一切修改被拒绝；解盲需法定票数 + 秘书授权，且不可逆；
- 迟到意见与迟到票保存为旁注，绝不悄悄计票；
- 决议携带可核对的有效票数、异议链与采用的确切方案版本。
"""

from __future__ import annotations

import hmac
import math
from datetime import datetime

from .errors import (
    AccessDenied,
    AuthorizationError,
    CaseSealedError,
    InvalidState,
    QuorumNotMet,
    WindowClosed,
)
from .notifications import assert_identity_safe, build_reminder, routing_event
from .store import GENESIS, EventStore, digest

REVIEW_ROLES = ("reviewer", "conservation_scientist")
DECISION_ROLES = ("committee_chair", "secretary")


def _iso(at) -> str:
    return at.isoformat() if isinstance(at, datetime) else str(at)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


class BlindReviewEngine:
    def __init__(self, store: EventStore, contract: dict, vault=None, secretary_token: str = ""):
        self.store = store
        self.contract = contract
        self.vault = vault
        self._secretary_token = secretary_token
        self.cases: dict[str, dict] = {}
        for event in store.events:  # 崩溃恢复：重放日志，封存结果随之还原
            self._apply(event)

    # ------------------------------------------------------------------ 内部

    def _case(self, case_id: str) -> dict:
        try:
            return self.cases[case_id]
        except KeyError:
            raise InvalidState(f"未知案件: {case_id}") from None

    def _participant(self, case: dict, alias: str) -> dict:
        try:
            return case["participants"][alias]
        except KeyError:
            raise InvalidState(f"未注册的参与者: {alias}") from None

    def _append(self, type_: str, case_id: str, payload: dict) -> dict:
        event = self.store.append(type_, case_id, payload)
        self._apply(event)
        return event

    def _mutable(self, case: dict) -> None:
        if case["state"] in ("sealed", "unblinded"):
            raise CaseSealedError("案件已封存，结果不可更改")

    def _check_secretary(self, token: str) -> None:
        if not self._secretary_token or not hmac.compare_digest(token or "", self._secretary_token):
            raise AuthorizationError("需要秘书授权")

    def _check_identity_safe(self, case: dict, event: dict) -> None:
        strings = self.vault.identity_strings(case["case_id"]) if self.vault else []
        assert_identity_safe(event, self.contract["identity_forbidden_fields"], strings)

    def _apply(self, event: dict) -> None:
        type_, payload, case_id = event["type"], event["payload"], event["case_id"]
        if type_ == "case_opened":
            self.cases[case_id] = {
                "case_id": case_id,
                "sensitivity": payload["sensitivity"],
                "state": "collecting_disclosures",
                "windows": payload["windows"],
                "opened_at": payload["at"],
                "participants": {},
                "proposal_versions": [],
                "opinions": {},
                "inquiries": {},
                "ballots": {},
                "late_votes": [],
                "retracted_vote_ids": [],
                "counters": {"opinion": 0, "vote": 0, "inquiry": 0},
                "seal": None,
                "seal_hash": None,
                "resolution": None,
            }
            return
        case = self.cases[case_id]
        if type_ == "participant_registered":
            case["participants"][payload["alias"]] = {"role": payload["role"], "conflict_state": payload["conflict_state"]}
        elif type_ == "disclosure_submitted":
            case["participants"][payload["alias"]]["conflict_state"] = payload["resulting_state"]
        elif type_ == "conflict_decided":
            case["participants"][payload["alias"]]["conflict_state"] = payload["decision"]
            if payload["decision"] == "recused":
                # 旧意见保留但标记为 excluded，不再计入法定人数与异议链
                for opinion in case["opinions"].values():
                    if opinion["alias"] == payload["alias"] and opinion["state"] == "submitted":
                        opinion["state"] = "excluded"
                        opinion["excluded_reason"] = "recused"
        elif type_ == "proposal_submitted":
            case["proposal_versions"].append(
                {
                    "version": payload["version"],
                    "sections": payload["sections"],
                    "attachments": payload["attachments"],
                    "submitted_at": payload["at"],
                }
            )
            for opinion_id in payload.get("reopened_opinion_ids", []):
                opinion = case["opinions"][opinion_id]
                opinion["state"] = "draft"
                opinion["reopened_due_to"] = payload["affected_sections"]
            for opinion_id in payload.get("carried_opinion_ids", []):
                case["opinions"][opinion_id]["proposal_version"] = payload["version"]
        elif type_ == "opinion_submitted":
            case["counters"]["opinion"] += 1
            case["opinions"][payload["opinion_id"]] = {
                "opinion_id": payload["opinion_id"],
                "alias": payload["alias"],
                "proposal_version": payload["proposal_version"],
                "section_refs": payload["section_refs"],
                "stance": payload["stance"],
                "text": payload["text"],
                "state": payload["state"],
                "submitted_at": payload["at"],
            }
        elif type_ == "opinion_resubmitted":
            opinion = case["opinions"][payload["opinion_id"]]
            opinion.update(
                proposal_version=payload["proposal_version"],
                section_refs=payload["section_refs"],
                stance=payload["stance"],
                text=payload["text"],
                state=payload["state"],
                submitted_at=payload["at"],
            )
            opinion.pop("reopened_due_to", None)
        elif type_ == "opinion_withdrawn":
            case["opinions"][payload["opinion_id"]]["state"] = "withdrawn"
        elif type_ == "inquiry_submitted":
            case["counters"]["inquiry"] += 1
            case["inquiries"][payload["inquiry_id"]] = {
                "inquiry_id": payload["inquiry_id"],
                "alias": payload["alias"],
                "section_refs": payload["section_refs"],
                "question": payload["question"],
                "asked_at": payload["at"],
                "answer": None,
                "answered_at": None,
            }
        elif type_ == "inquiry_answered":
            inquiry = case["inquiries"][payload["inquiry_id"]]
            inquiry["answer"] = payload["answer"]
            inquiry["answered_at"] = payload["at"]
        elif type_ == "vote_cast":
            case["counters"]["vote"] += 1
            case["ballots"][payload["vote"]["reviewer_alias"]] = payload["vote"]
        elif type_ == "vote_late":
            case["counters"]["vote"] += 1
            case["late_votes"].append(payload["vote"])
        elif type_ == "vote_retracted":
            ballot = case["ballots"].get(payload["alias"])
            if ballot and ballot["vote_id"] == payload["vote_id"]:
                del case["ballots"][payload["alias"]]
            case["retracted_vote_ids"].append(payload["vote_id"])
        elif type_ == "stage_advanced":
            case["state"] = payload["to"]
        elif type_ == "case_sealed":
            case["state"] = "sealed"
            case["seal"] = payload["snapshot"]
            case["seal_hash"] = event["hash"]
        elif type_ == "case_unblinded":
            case["state"] = "unblinded"
        elif type_ == "resolution_published":
            case["resolution"] = payload["resolution"]

    def _advance(self, case_id: str, to: str, at) -> None:
        with self.store.lock:
            case = self._case(case_id)
            order = self.contract["case_states"]
            if order.index(to) != order.index(case["state"]) + 1:
                raise InvalidState(f"不能从 {case['state']} 转到 {to}")
            self._append("stage_advanced", case_id, {"from": case["state"], "to": to, "at": _iso(at)})

    # ------------------------------------------------------------------ 案件与参与者

    def open_case(self, case_id: str, sensitivity: str, windows: dict, at) -> dict:
        with self.store.lock:
            if case_id in self.cases:
                raise InvalidState(f"案件已存在: {case_id}")
            if sensitivity not in self.contract["sensitivity_levels"]:
                raise InvalidState(f"未知敏感级别: {sensitivity}")
            self._append("case_opened", case_id, {"sensitivity": sensitivity, "windows": windows, "at": _iso(at)})
            return self.cases[case_id]

    def register_participant(self, case_id: str, alias: str, role: str) -> None:
        with self.store.lock:
            case = self._case(case_id)
            self._mutable(case)
            if role not in self.contract["roles"]:
                raise InvalidState(f"未知角色: {role}")
            if alias in case["participants"]:
                raise InvalidState(f"别名已存在: {alias}")
            conflict_state = "undeclared" if role in REVIEW_ROLES else "clear"
            self._append("participant_registered", case_id, {"alias": alias, "role": role, "conflict_state": conflict_state})

    def open_review(self, case_id: str, at) -> None:
        self._advance(case_id, "review_open", at)

    def open_questions(self, case_id: str, at) -> None:
        self._advance(case_id, "questions_open", at)

    def open_voting(self, case_id: str, at) -> None:
        with self.store.lock:
            case = self._case(case_id)
            if not case["proposal_versions"]:
                raise InvalidState("尚无方案版本，不能进入投票")
            self._advance(case_id, "voting", at)

    # ------------------------------------------------------------------ 冲突申报与回避

    def submit_disclosure(self, case_id: str, alias: str, has_conflict: bool, at) -> None:
        with self.store.lock:
            case = self._case(case_id)
            self._mutable(case)
            participant = self._participant(case, alias)
            if participant["role"] not in REVIEW_ROLES:
                raise InvalidState("该角色无需申报利益冲突")
            if participant["conflict_state"] == "recused":
                raise InvalidState("已回避，申报不改变回避状态")
            resulting = "pending_decision" if has_conflict else "clear"
            self._append(
                "disclosure_submitted",
                case_id,
                {"alias": alias, "has_conflict": has_conflict, "resulting_state": resulting, "at": _iso(at)},
            )

    def decide_conflict(self, case_id: str, alias: str, decision: str, by_role: str, at) -> None:
        with self.store.lock:
            case = self._case(case_id)
            self._mutable(case)
            if by_role not in DECISION_ROLES:
                raise AuthorizationError("仅主任委员或秘书可裁定利益冲突")
            if decision not in ("clear", "recused"):
                raise InvalidState(f"未知裁定: {decision}")
            participant = self._participant(case, alias)
            if participant["conflict_state"] == "recused":
                raise InvalidState("该专家已处于回避状态")
            self._append("conflict_decided", case_id, {"alias": alias, "decision": decision, "by": by_role, "at": _iso(at)})

    # ------------------------------------------------------------------ 方案版本

    @staticmethod
    def _affected_sections(old_sections: list, new_sections: list) -> set:
        old = {s["section_id"]: s for s in old_sections}
        new = {s["section_id"]: s for s in new_sections}
        affected = set()
        for section_id, section in new.items():
            if section_id in old and section.get("is_key") and section["content"] != old[section_id]["content"]:
                affected.add(section_id)
        affected |= set(old) - set(new)  # 被删除的段落同样影响引用它的评审
        return affected

    def submit_proposal(self, case_id: str, author_alias: str, sections: list, attachments: list, at) -> int:
        with self.store.lock:
            case = self._case(case_id)
            self._mutable(case)
            if case["state"] == "voting":
                raise InvalidState("投票期间不可修改方案")
            if self._participant(case, author_alias)["role"] != "proposal_author":
                raise AuthorizationError("仅方案作者可提交方案")
            version = len(case["proposal_versions"]) + 1
            payload = {"version": version, "sections": sections, "attachments": attachments, "by": author_alias, "at": _iso(at)}
            if version > 1:
                affected = self._affected_sections(case["proposal_versions"][-1]["sections"], sections)
                reopened, carried = [], []
                for opinion in case["opinions"].values():
                    if opinion["state"] != "submitted":
                        continue
                    (reopened if set(opinion["section_refs"]) & affected else carried).append(opinion["opinion_id"])
                payload.update(
                    {
                        "affected_sections": sorted(affected),
                        "reopened_opinion_ids": sorted(reopened),
                        "carried_opinion_ids": sorted(carried),
                    }
                )
            self._append("proposal_submitted", case_id, payload)
            return version

    def get_proposal(self, case_id: str, alias: str) -> dict:
        case = self._case(case_id)
        participant = self._participant(case, alias)
        if participant["role"] in REVIEW_ROLES and participant["conflict_state"] != "clear":
            raise AccessDenied("未完成利益冲突申报或已被回避，不能接触方案内容")
        if not case["proposal_versions"]:
            raise InvalidState("尚无方案版本")
        return case["proposal_versions"][-1]

    # ------------------------------------------------------------------ 独立意见

    def submit_opinion(self, case_id: str, alias: str, section_refs: list, stance: str, text: str, at, opinion_id: str = None):
        with self.store.lock:
            case = self._case(case_id)
            self._mutable(case)
            participant = self._participant(case, alias)
            if participant["role"] not in REVIEW_ROLES:
                raise AuthorizationError("仅评审角色可提交意见")
            if participant["conflict_state"] != "clear":
                raise AccessDenied("未完成利益冲突申报或已被回避，不能提交意见")
            if case["state"] not in ("review_open", "questions_open", "voting"):
                raise InvalidState("意见窗口未开启")
            if not case["proposal_versions"]:
                raise InvalidState("尚无方案版本")
            if stance not in self.contract["opinion_stances"]:
                raise InvalidState(f"未知意见立场: {stance}")
            current = case["proposal_versions"][-1]
            known = {s["section_id"] for s in current["sections"]}
            if not set(section_refs) <= known:
                raise InvalidState("意见引用了不存在的段落")
            # 迟到的意见保存为旁注（late_note），绝不悄悄计票
            state = "late_note" if _parse(_iso(at)) > _parse(case["windows"]["review"]["closes_at"]) else "submitted"
            if opinion_id is None:
                opinion_id = f"opinion-{case['counters']['opinion'] + 1}"
                self._append(
                    "opinion_submitted",
                    case_id,
                    {
                        "opinion_id": opinion_id,
                        "alias": alias,
                        "proposal_version": current["version"],
                        "section_refs": list(section_refs),
                        "stance": stance,
                        "text": text,
                        "state": state,
                        "at": _iso(at),
                    },
                )
            else:
                opinion = case["opinions"].get(opinion_id)
                if not opinion or opinion["alias"] != alias:
                    raise InvalidState(f"无此意见: {opinion_id}")
                if opinion["state"] != "draft":
                    raise InvalidState("仅被重开的意见可重新提交")
                self._append(
                    "opinion_resubmitted",
                    case_id,
                    {
                        "opinion_id": opinion_id,
                        "alias": alias,
                        "proposal_version": current["version"],
                        "section_refs": list(section_refs),
                        "stance": stance,
                        "text": text,
                        "state": state,
                        "at": _iso(at),
                    },
                )
            return opinion_id, state

    def withdraw_opinion(self, case_id: str, alias: str, opinion_id: str, at) -> None:
        with self.store.lock:
            case = self._case(case_id)
            self._mutable(case)
            opinion = case["opinions"].get(opinion_id)
            if not opinion or opinion["alias"] != alias:
                raise InvalidState(f"无此意见: {opinion_id}")
            if opinion["state"] not in ("submitted", "draft", "late_note"):
                raise InvalidState(f"当前状态不可撤回: {opinion['state']}")
            self._append("opinion_withdrawn", case_id, {"opinion_id": opinion_id, "alias": alias, "at": _iso(at)})

    # ------------------------------------------------------------------ 质询与答复

    def submit_inquiry(self, case_id: str, alias: str, section_refs: list, question: str, at):
        with self.store.lock:
            case = self._case(case_id)
            self._mutable(case)
            participant = self._participant(case, alias)
            if participant["role"] not in REVIEW_ROLES:
                raise AuthorizationError("仅评审角色可发起质询")
            if participant["conflict_state"] != "clear":
                raise AccessDenied("未完成利益冲突申报或已被回避，不能发起质询")
            if case["state"] != "questions_open":
                raise InvalidState("不在质询阶段")
            if _parse(_iso(at)) > _parse(case["windows"]["questions"]["closes_at"]):
                raise WindowClosed("质询窗口已关闭")
            inquiry_id = f"inquiry-{case['counters']['inquiry'] + 1}"
            self._append(
                "inquiry_submitted",
                case_id,
                {"inquiry_id": inquiry_id, "alias": alias, "section_refs": list(section_refs), "question": question, "at": _iso(at)},
            )
            event = routing_event("question_routed", case_id, "author", {"inquiry_id": inquiry_id})
            self._check_identity_safe(case, event)
            return inquiry_id, [event]

    def answer_inquiry(self, case_id: str, author_alias: str, inquiry_id: str, answer: str, at):
        with self.store.lock:
            case = self._case(case_id)
            self._mutable(case)
            if self._participant(case, author_alias)["role"] != "proposal_author":
                raise AuthorizationError("仅方案作者可答复质询")
            if case["state"] != "questions_open":
                raise InvalidState("不在质询阶段")
            if _parse(_iso(at)) > _parse(case["windows"]["questions"]["closes_at"]):
                raise WindowClosed("质询窗口已关闭")
            inquiry = case["inquiries"].get(inquiry_id)
            if not inquiry:
                raise InvalidState(f"无此质询: {inquiry_id}")
            self._append("inquiry_answered", case_id, {"inquiry_id": inquiry_id, "answer": answer, "at": _iso(at)})
            event = routing_event("response_routed", case_id, inquiry["alias"], {"inquiry_id": inquiry_id})
            self._check_identity_safe(case, event)
            return [event]

    # ------------------------------------------------------------------ 投票

    def cast_vote(self, case_id: str, alias: str, choice: str, at):
        with self.store.lock:
            case = self._case(case_id)
            self._mutable(case)
            if case["state"] != "voting":
                raise InvalidState("不在投票阶段")
            if choice not in self.contract["vote_choices"]:
                raise InvalidState(f"未知表决项: {choice}")
            participant = self._participant(case, alias)
            if participant["role"] not in REVIEW_ROLES:
                raise AuthorizationError("仅评审角色可投票")
            if participant["conflict_state"] != "clear":
                raise AccessDenied("未完成利益冲突申报或已被回避，不能投票")
            vote = {
                "vote_id": f"vote-{case['counters']['vote'] + 1}",
                "case_id": case_id,
                "proposal_version": case["proposal_versions"][-1]["version"],
                "reviewer_alias": alias,
                "choice": choice,
                "cast_at": _iso(at),
            }
            if _parse(_iso(at)) > _parse(case["windows"]["voting"]["closes_at"]):
                # 迟到票保存为旁注，不计入票数
                self._append("vote_late", case_id, {"vote": vote, "counted": False})
                return vote["vote_id"], False
            self._append("vote_cast", case_id, {"vote": vote})
            return vote["vote_id"], True

    def retract_vote(self, case_id: str, alias: str, vote_id: str, at) -> None:
        with self.store.lock:
            case = self._case(case_id)
            self._mutable(case)
            if case["state"] != "voting":
                raise InvalidState("不在投票阶段")
            if _parse(_iso(at)) > _parse(case["windows"]["voting"]["closes_at"]):
                raise WindowClosed("投票窗口已关闭")
            ballot = case["ballots"].get(alias)
            if not ballot or ballot["vote_id"] != vote_id:
                raise InvalidState("无有效票可撤回")
            self._append("vote_retracted", case_id, {"alias": alias, "vote_id": vote_id, "at": _iso(at)})

    # ------------------------------------------------------------------ 封存、解盲、决议

    def _tally_snapshot(self, case: dict) -> dict:
        eligible = sorted(
            alias
            for alias, p in case["participants"].items()
            if p["role"] in REVIEW_ROLES and p["conflict_state"] == "clear"
        )
        version = case["proposal_versions"][-1]["version"]
        valid = {a: v for a, v in case["ballots"].items() if a in eligible and v["proposal_version"] == version}
        tally = {choice: 0 for choice in self.contract["vote_choices"]}
        for vote in valid.values():
            tally[vote["choice"]] += 1
        quorum = self.contract["quorum"]
        required = max(quorum["minimum"], math.ceil(quorum["numerator"] * len(eligible) / quorum["denominator"]))
        return {
            "proposal_version": version,
            "eligible": eligible,
            "valid_votes": len(valid),
            "valid_vote_ids": sorted(v["vote_id"] for v in valid.values()),
            "tally": tally,
            "quorum_required": required,
            "quorum_met": len(valid) >= required,
        }

    def seal(self, case_id: str, at) -> dict:
        with self.store.lock:
            case = self._case(case_id)
            if case["state"] != "voting":
                raise InvalidState("仅投票阶段可封存")
            if _parse(_iso(at)) < _parse(case["windows"]["voting"]["closes_at"]):
                raise InvalidState("投票窗口尚未关闭")
            snapshot = self._tally_snapshot(case)
            self._append("case_sealed", case_id, {"snapshot": snapshot, "at": _iso(at)})
            return case["seal"]

    def unblind(self, case_id: str, secretary_token: str, at) -> None:
        with self.store.lock:
            self._check_secretary(secretary_token)
            case = self._case(case_id)
            if case["state"] == "unblinded":
                raise InvalidState("案件已解盲，解盲不可逆")
            if case["state"] != "sealed":
                raise InvalidState("仅封存后可解盲")
            if not case["seal"]["quorum_met"]:
                raise QuorumNotMet("有效票数未达法定人数，不能解盲")
            self._append("case_unblinded", case_id, {"by": "secretary", "at": _iso(at)})

    def _dissent_chain(self, case: dict, seal: dict) -> list:
        entries = []
        for opinion in case["opinions"].values():
            if opinion["state"] == "submitted" and opinion["stance"] == "object":
                entries.append(
                    {
                        "kind": "opinion",
                        "ref": opinion["opinion_id"],
                        "alias": opinion["alias"],
                        "section_refs": opinion["section_refs"],
                        "at": opinion["submitted_at"],
                    }
                )
        for alias, vote in case["ballots"].items():
            if alias in seal["eligible"] and vote["proposal_version"] == seal["proposal_version"] and vote["choice"] == "reject":
                entries.append({"kind": "vote", "ref": vote["vote_id"], "alias": alias, "section_refs": [], "at": vote["cast_at"]})
        entries.sort(key=lambda entry: (entry["at"], entry["ref"]))
        prev = GENESIS
        for entry in entries:
            entry["prev_digest"] = prev
            entry["digest"] = digest(entry)
            prev = entry["digest"]
        return entries

    def _build_resolution(self, case: dict, published_at: str) -> dict:
        seal = case["seal"]
        tally, valid = seal["tally"], seal["valid_votes"]
        approve, conditional = tally.get("approve", 0), tally.get("approve_with_conditions", 0)
        if valid and approve > valid / 2:
            decision = "adopted"
        elif valid and approve + conditional > valid / 2:
            decision = "adopted_with_conditions"
        elif valid:
            decision = "rejected"
        else:
            decision = "no_decision"
        resolution = {
            "case_id": case["case_id"],
            "proposal_version": seal["proposal_version"],
            "decision": decision,
            "tally": tally,
            "valid_votes": valid,
            "valid_vote_ids": seal["valid_vote_ids"],
            "quorum": {"required": seal["quorum_required"], "met": seal["quorum_met"], "eligible": len(seal["eligible"])},
            "dissent_chain": self._dissent_chain(case, seal),
            "excluded_opinion_ids": sorted(o["opinion_id"] for o in case["opinions"].values() if o["state"] == "excluded"),
            "late_note_ids": sorted(o["opinion_id"] for o in case["opinions"].values() if o["state"] == "late_note"),
            "late_vote_ids": sorted(v["vote_id"] for v in case["late_votes"]),
            "seal_hash": case["seal_hash"],
            "published_at": published_at,
        }
        resolution["resolution_hash"] = digest(resolution)
        return resolution

    def publish_resolution(self, case_id: str, secretary_token: str, at) -> dict:
        with self.store.lock:
            self._check_secretary(secretary_token)
            case = self._case(case_id)
            if case["state"] not in ("sealed", "unblinded"):
                raise InvalidState("决议只能在封存后发布")
            if not case["seal"]["quorum_met"]:
                raise QuorumNotMet("有效票数未达法定人数，不能发布决议")
            if case["resolution"]:
                return case["resolution"]
            resolution = self._build_resolution(case, _iso(at))
            self._append("resolution_published", case_id, {"resolution": resolution})
            return resolution

    def get_resolution(self, case_id: str) -> dict | None:
        return self._case(case_id)["resolution"]

    def verify_resolution(self, case_id: str) -> bool:
        """从事故日志重算决议：有效票数、异议链、方案版本与封存哈希全部可核对。"""
        case = self._case(case_id)
        stored = case["resolution"]
        if not stored:
            raise InvalidState("决议尚未发布")
        seal_events = [e for e in self.store.events if e["case_id"] == case_id and e["type"] == "case_sealed"]
        if not seal_events or seal_events[-1]["hash"] != case["seal_hash"]:
            return False
        recomputed = self._build_resolution(case, stored["published_at"])
        return recomputed["resolution_hash"] == stored["resolution_hash"]

    # ------------------------------------------------------------------ 提醒与身份

    def pending_reminders(self, case_id: str, at, retry: int = 0) -> list:
        """生成待办提醒；无论重试多少次，载荷只含案件引用与别名。"""
        case = self._case(case_id)
        windows = case["windows"]
        events = []
        reviewers = sorted((a, p) for a, p in case["participants"].items() if p["role"] in REVIEW_ROLES)
        for alias, participant in reviewers:
            if participant["conflict_state"] in ("undeclared", "pending_decision"):
                events.append(build_reminder("disclosure_reminder", case_id, alias, windows["disclosure"], retry))
        if case["state"] in ("review_open", "questions_open"):
            for alias, participant in reviewers:
                if participant["conflict_state"] != "clear":
                    continue
                submitted = any(o["alias"] == alias and o["state"] == "submitted" for o in case["opinions"].values())
                if not submitted:
                    events.append(build_reminder("review_reminder", case_id, alias, windows["review"], retry))
        if case["state"] == "voting":
            for alias, participant in reviewers:
                if participant["conflict_state"] == "clear" and alias not in case["ballots"]:
                    events.append(build_reminder("voting_reminder", case_id, alias, windows["voting"], retry))
        for event in events:
            self._check_identity_safe(case, event)
        return events

    def resolve_identity(self, case_id: str, alias: str, requester_role: str) -> dict:
        if not self.vault:
            raise AccessDenied("未配置身份库")
        case = self._case(case_id)
        return self.vault.resolve(
            case_id,
            alias,
            requester_role=requester_role,
            sensitivity=case["sensitivity"],
            unblinded=case["state"] == "unblinded",
        )
