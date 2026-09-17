"""盲审领域规则测试：申报门控、回避、重开、解盲、通知、旁注、决议与恢复。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from blind_review import (  # noqa: E402
    AccessDeniedError,
    Actor,
    AuthorizationError,
    BlindReviewService,
    CaseCalendar,
    CaseSealedError,
    CaseStateError,
    DuplicateVoteError,
    EventStore,
    Identity,
    IdentityLeakError,
    IdentityRegistry,
    IdentitySealError,
    QuorumNotMetError,
    WindowError,
    load_contract,
    parse_dt,
)

SECRETARY = Actor(role="secretary", alias="secretary-1")
CHAIR = Actor(role="committee_chair", alias="chair-1")
AUTHOR = Actor(role="proposal_author", alias="author-1")
A = Actor(role="reviewer", alias="reviewer-a")
B = Actor(role="reviewer", alias="reviewer-b")
C = Actor(role="reviewer", alias="reviewer-c")

CASE_ID = "case-2026-31"
TOKEN = "sec-token-2026"

SECTIONS = [
    {"section_id": "condition-report", "title": "保存状况", "content": "鎏金剥落", "key": False},
    {"section_id": "cleaning", "title": "清洗方案", "content": "弱酸清洗", "key": True},
    {"section_id": "infill", "title": "补全材料", "content": "矿物填料A", "key": True},
    {"section_id": "monitoring", "title": "监测计划", "content": "年度巡检", "key": False},
]

CALENDAR = {
    "disclosure": {"start": "2026-09-01T09:00:00+08:00", "end": "2026-09-05T18:00:00+08:00"},
    "review": {"start": "2026-09-06T09:00:00+08:00", "end": "2026-09-12T18:00:00+08:00"},
    "questions": {"start": "2026-09-13T09:00:00+08:00", "end": "2026-09-14T18:00:00+08:00"},
    "voting": {"start": "2026-09-15T09:00:00+08:00", "end": "2026-09-16T18:00:00+08:00"},
}

T = {
    "register": "2026-09-01T09:00:00+08:00",
    "disclose": "2026-09-01T10:00:00+08:00",
    "review": "2026-09-08T11:00:00+08:00",
    "review_open": "2026-09-06T09:00:00+08:00",
    "questions": "2026-09-13T10:00:00+08:00",
    "questions_open": "2026-09-13T09:00:00+08:00",
    "voting": "2026-09-15T10:00:00+08:00",
    "voting_open": "2026-09-15T09:00:00+08:00",
    "seal": "2026-09-16T19:00:00+08:00",
    "unblind": "2026-09-17T09:00:00+08:00",
}


def at(moment: str):
    return parse_dt(moment)


class ServiceFixture(unittest.TestCase):
    """搭好一个已完成申报、可自由推进阶段的案件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tmp.name) / "events.jsonl"
        self.contract = load_contract()
        self.registry = IdentityRegistry()
        self.registry.register(Identity(alias="reviewer-a", real_name="王岚", contact="wang@example.org"))
        self.registry.register(Identity(alias="reviewer-b", real_name="陈默"))
        self.registry.register(Identity(alias="reviewer-c", real_name="李青"))
        self.registry.register(Identity(alias="author-1", real_name="赵远"))
        self.service = self._new_service()

    def tearDown(self):
        self.tmp.cleanup()

    def _new_service(self) -> BlindReviewService:
        return BlindReviewService(EventStore(self.log_path), self.contract, self.registry)

    def register(self, sensitivity="restricted", quorum=2) -> None:
        self.service.register_case(
            actor=SECRETARY,
            case_id=CASE_ID,
            sensitivity=sensitivity,
            calendar=CaseCalendar.from_dict(CALENDAR),
            author_alias="author-1",
            reviewer_aliases=["reviewer-a", "reviewer-b", "reviewer-c"],
            sections=SECTIONS,
            quorum=quorum,
            secretary_token=TOKEN,
            now=at(T["register"]),
        )

    def disclose_all(self) -> None:
        for actor in (A, B, C):
            self.service.submit_disclosure(
                actor=actor, case_id=CASE_ID, statement="无利益关系",
                has_conflict=False, now=at(T["disclose"]),
            )

    def open_review(self) -> None:
        self.service.advance_phase(actor=SECRETARY, case_id=CASE_ID, now=at(T["review_open"]))

    def open_questions(self) -> None:
        self.service.advance_phase(actor=SECRETARY, case_id=CASE_ID, now=at(T["questions_open"]))

    def open_voting(self) -> None:
        self.service.advance_phase(actor=SECRETARY, case_id=CASE_ID, now=at(T["voting_open"]))

    def to_voting(self) -> None:
        self.register()
        self.disclose_all()
        self.open_review()
        self.open_questions()
        self.open_voting()

    def vote(self, actor, vote_id, choice="approve", moment=None):
        return self.service.cast_vote(
            actor=actor, case_id=CASE_ID, vote_id=vote_id, choice=choice,
            now=at(moment or T["voting"]),
        )

    def seal(self):
        return self.service.seal_case(actor=SECRETARY, case_id=CASE_ID, now=at(T["seal"]))


class TestDisclosureGate(ServiceFixture):
    def test_undeclared_reviewer_cannot_read_proposal(self):
        self.register()
        with self.assertRaises(AccessDeniedError):
            self.service.get_proposal_content(actor=A, case_id=CASE_ID)

    def test_pending_decision_cannot_read_until_cleared(self):
        self.register()
        state = self.service.submit_disclosure(
            actor=A, case_id=CASE_ID, statement="与供应方有合作",
            has_conflict=True, now=at(T["disclose"]),
        )
        self.assertEqual(state, "pending_decision")
        self.open_review()
        with self.assertRaises(AccessDeniedError):
            self.service.get_proposal_content(actor=A, case_id=CASE_ID)
        self.service.decide_disclosure(
            actor=CHAIR, case_id=CASE_ID, alias="reviewer-a",
            decision="clear", reason="合作已终止三年", now=at(T["review"]),
        )
        content = self.service.get_proposal_content(actor=A, case_id=CASE_ID)
        self.assertEqual(content["proposal_version"], 1)

    def test_recused_reviewer_loses_access(self):
        self.register()
        self.disclose_all()
        self.service.recuse_reviewer(
            actor=CHAIR, case_id=CASE_ID, alias="reviewer-b",
            reason="与供应方合作", now=at(T["disclose"]),
        )
        self.open_review()
        with self.assertRaises(AccessDeniedError):
            self.service.get_proposal_content(actor=B, case_id=CASE_ID)

    def test_clear_reviewer_reads_after_review_opens(self):
        self.register()
        self.disclose_all()
        self.open_review()
        content = self.service.get_proposal_content(actor=A, case_id=CASE_ID)
        self.assertIn("sections", content)
        self.assertNotIn("real_name", str(content))  # 内容视图不含身份字段


class TestRecusal(ServiceFixture):
    def test_recusal_retains_opinion_but_excludes_it(self):
        self.register()
        self.disclose_all()
        self.open_review()
        self.service.submit_opinion(
            actor=B, case_id=CASE_ID, opinion_id="op-b", sections=["infill"],
            recommendation="approve", text="建议采用供应方X", now=at(T["review"]),
        )
        self.service.recuse_reviewer(
            actor=CHAIR, case_id=CASE_ID, alias="reviewer-b",
            reason="与材料供应方合作", now=at(T["review"]),
        )
        opinion = self.service.get_case(CASE_ID).opinions["op-b"]
        self.assertEqual(opinion.state, "excluded")  # 保留但排除

    def test_recused_vote_excluded_from_quorum(self):
        self.to_voting()
        self.vote(A, "vote-1")
        self.vote(B, "vote-2")
        self.service.recuse_reviewer(
            actor=CHAIR, case_id=CASE_ID, alias="reviewer-b",
            reason="申报后才发现的合作关系", now=at(T["voting"]),
        )
        sealed = self.seal()
        self.assertEqual([v["vote_id"] for v in sealed.valid_votes], ["vote-1"])
        self.assertEqual(
            {e["vote_id"]: e["reason"] for e in sealed.excluded_votes},
            {"vote-2": "recused_or_undeclared"},
        )
        self.assertFalse(sealed.quorum_met)  # quorum=2，只剩 1 张有效票

    def test_recusal_after_seal_rejected(self):
        self.to_voting()
        self.vote(A, "vote-1")
        self.vote(C, "vote-3")
        self.seal()
        with self.assertRaises(CaseSealedError):
            self.service.recuse_reviewer(
                actor=CHAIR, case_id=CASE_ID, alias="reviewer-b",
                reason="封存后才曝光", now=at(T["seal"]),
            )


class TestAmendmentReopen(ServiceFixture):
    def setUp(self):
        super().setUp()
        self.register()
        self.disclose_all()
        self.open_review()
        self.service.submit_opinion(
            actor=A, case_id=CASE_ID, opinion_id="op-1", sections=["cleaning", "infill"],
            recommendation="approve_with_conditions", text="清洗需降酸度", now=at(T["review"]),
        )
        self.service.submit_opinion(
            actor=C, case_id=CASE_ID, opinion_id="op-3", sections=["monitoring"],
            recommendation="approve", text="监测合理", now=at(T["review"]),
        )

    def amend(self, mutate):
        sections = [dict(s) for s in SECTIONS]
        mutate(sections)
        return self.service.amend_proposal(
            actor=AUTHOR, case_id=CASE_ID, sections=sections, now=at(T["review"]),
        )

    def test_key_section_change_reopens_only_affected(self):
        result = self.amend(lambda s: s[1].update(content="激光清洗"))
        self.assertEqual(result["changed_key_sections"], ["cleaning"])
        self.assertEqual(result["reopened_opinions"], ["op-1"])  # op-3 不涉及 cleaning
        case = self.service.get_case(CASE_ID)
        self.assertEqual(case.opinions["op-1"].state, "draft")
        self.assertEqual(case.opinions["op-1"].reopen_history[0]["changed_sections"], ["cleaning"])
        self.assertEqual(case.opinions["op-3"].state, "submitted")

    def test_non_key_change_reopens_nothing(self):
        result = self.amend(lambda s: s[0].update(content="鎏金剥落加剧"))
        self.assertEqual(result["changed_key_sections"], [])
        self.assertEqual(result["reopened_opinions"], [])

    def test_reopened_opinion_resubmits_on_new_version(self):
        self.amend(lambda s: s[1].update(content="激光清洗"))
        state = self.service.submit_opinion(
            actor=A, case_id=CASE_ID, opinion_id="op-1", sections=["cleaning", "infill"],
            recommendation="approve", text="激光方案可行", now=at(T["review"]),
        )
        self.assertEqual(state, "submitted")
        opinion = self.service.get_case(CASE_ID).opinions["op-1"]
        self.assertEqual(opinion.proposal_version, 2)

    def test_amendment_rejected_during_voting(self):
        self.open_questions()
        self.open_voting()
        with self.assertRaises(CaseStateError):
            self.amend(lambda s: s[1].update(content="激光清洗"))


class TestUnblind(ServiceFixture):
    def test_quorum_required(self):
        self.to_voting()
        self.vote(A, "vote-1")  # 只有 1 票，quorum=2
        self.seal()
        with self.assertRaises(QuorumNotMetError):
            self.service.unblind_case(actor=SECRETARY, case_id=CASE_ID, token=TOKEN, now=at(T["unblind"]))

    def test_secretary_authorization_required(self):
        self.to_voting()
        self.vote(A, "vote-1")
        self.vote(C, "vote-3")
        self.seal()
        with self.assertRaises(AuthorizationError):
            self.service.unblind_case(actor=SECRETARY, case_id=CASE_ID, token="bad", now=at(T["unblind"]))
        with self.assertRaises(AuthorizationError):
            self.service.unblind_case(actor=CHAIR, case_id=CASE_ID, token=TOKEN, now=at(T["unblind"]))

    def test_unblind_is_irreversible(self):
        self.to_voting()
        self.vote(A, "vote-1")
        self.vote(C, "vote-3")
        self.seal()
        self.service.unblind_case(actor=SECRETARY, case_id=CASE_ID, token=TOKEN, now=at(T["unblind"]))
        with self.assertRaises(CaseSealedError):
            self.service.unblind_case(actor=SECRETARY, case_id=CASE_ID, token=TOKEN, now=at(T["unblind"]))
        with self.assertRaises(CaseSealedError):
            self.vote(C, "vote-9", choice="reject")
        with self.assertRaises(CaseSealedError):
            self.service.submit_opinion(
                actor=C, case_id=CASE_ID, opinion_id="op-late", sections=["monitoring"],
                recommendation="reject", text="解盲后补意见", now=at(T["unblind"]),
            )


class TestReminders(ServiceFixture):
    def test_retry_is_idempotent_and_identity_safe(self):
        self.register()
        self.disclose_all()
        first = self.service.remind(
            case_id=CASE_ID, event_type="review_reminder",
            recipient_alias="reviewer-c", now=at(T["review"]),
        )
        retry = self.service.remind(
            case_id=CASE_ID, event_type="review_reminder",
            recipient_alias="reviewer-c", now=at(T["review"]),
        )
        self.assertEqual(first.event_id, retry.event_id)
        self.assertEqual(retry.attempt, 2)
        stable = set(first.to_payload()) - {"attempt", "scheduled_for"}
        for key in stable:
            self.assertEqual(first.to_payload()[key], retry.to_payload()[key])
        self.assertEqual(self.registry.leak_scan(str(retry.to_payload())), [])

    def test_notification_with_real_name_blocked(self):
        self.register()
        with self.assertRaises(IdentityLeakError):
            self.service.notifier.send_reminder(
                case_id=CASE_ID, event_type="review_reminder", recipient_alias="reviewer-c",
                now=at(T["review"]), body_template="请李青尽快提交",
            )

    def test_unknown_event_type_rejected(self):
        self.register()
        self.disclose_all()
        with self.assertRaises(ValueError):
            self.service.remind(
                case_id=CASE_ID, event_type="phone_call",  # 未登记类型
                recipient_alias="reviewer-c", now=at(T["review"]),
            )


class TestLateOpinion(ServiceFixture):
    def test_late_opinion_kept_as_note_not_counted(self):
        self.to_voting()
        state = self.service.submit_opinion(
            actor=C, case_id=CASE_ID, opinion_id="op-late", sections=["monitoring"],
            recommendation="reject", text="窗口关闭后的补充", now=at(T["voting"]),
        )
        self.assertEqual(state, "late_note")
        self.vote(A, "vote-1")
        self.vote(C, "vote-3")
        self.seal()
        resolution = self.service.publish_resolution(actor=SECRETARY, case_id=CASE_ID, now=at(T["unblind"]))
        notes = [e for e in resolution["dissent_chain"] if e["kind"] == "late_note"]
        self.assertEqual(len(notes), 1)
        self.assertFalse(notes[0]["counted"])  # 旁注不计票
        self.assertEqual(resolution["tally"]["reject"], 0)  # 旁注的 reject 不进入计票

    def test_opinion_before_review_open_rejected(self):
        self.register()
        self.disclose_all()
        with self.assertRaises(CaseStateError):
            self.service.submit_opinion(
                actor=A, case_id=CASE_ID, opinion_id="op-early", sections=["cleaning"],
                recommendation="approve", text="评审未开放", now=at(T["disclose"]),
            )


class TestResolution(ServiceFixture):
    def build_resolution(self):
        self.register()
        self.disclose_all()
        self.open_review()
        self.service.submit_opinion(
            actor=A, case_id=CASE_ID, opinion_id="op-1", sections=["cleaning"],
            recommendation="approve_with_conditions", text="降酸度", now=at(T["review"]),
        )
        self.open_questions()
        self.service.raise_inquiry(
            actor=A, case_id=CASE_ID, inquiry_id="q-1", text="热影响数据？",
            now=at(T["questions"]),
        )
        self.service.submit_opinion(
            actor=C, case_id=CASE_ID, opinion_id="op-late", sections=["monitoring"],
            recommendation="reject", text="迟到意见", now=at("2026-09-13T16:00:00+08:00"),
        )
        self.open_voting()
        self.vote(A, "vote-1", choice="approve_with_conditions")
        self.vote(C, "vote-3", choice="approve")
        self.seal()
        return self.service.publish_resolution(actor=SECRETARY, case_id=CASE_ID, now=at(T["unblind"]))

    def test_resolution_contents(self):
        resolution = self.build_resolution()
        self.assertEqual(resolution["proposal_version"], 1)  # 采用的确切版本
        self.assertEqual(resolution["quorum"], {"required": 2, "valid_votes": 2, "met": True})
        self.assertEqual(resolution["valid_vote_ids"], ["vote-1", "vote-3"])
        kinds = [e["kind"] for e in resolution["dissent_chain"]]
        self.assertEqual(kinds, ["opinion", "inquiry", "late_note"])  # 按时间排序
        self.assertTrue(self.service.verify_resolution(case_id=CASE_ID, resolution=resolution))

    def test_tampered_resolution_fails_verification(self):
        resolution = self.build_resolution()
        forged = dict(resolution, tally={"approve": 2, "approve_with_conditions": 0, "reject": 0, "abstain": 0})
        self.assertFalse(self.service.verify_resolution(case_id=CASE_ID, resolution=forged))
        forged_version = dict(resolution, proposal_version=99)
        self.assertFalse(self.service.verify_resolution(case_id=CASE_ID, resolution=forged_version))

    def test_publish_is_idempotent(self):
        first = self.build_resolution()
        again = self.service.publish_resolution(actor=SECRETARY, case_id=CASE_ID, now=at(T["unblind"]))
        self.assertEqual(first, again)


class TestConcurrencyAndRecovery(ServiceFixture):
    def test_concurrent_same_vote_id_records_one_vote(self):
        self.to_voting()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda _: self.service.cast_vote(
                    actor=A, case_id=CASE_ID, vote_id="vote-1",
                    choice="approve", now=at(T["voting"]),
                ),
                range(16),
            ))
        self.assertEqual({r.vote_id for r in results}, {"vote-1"})
        self.assertEqual(len(self.service.get_case(CASE_ID).votes), 1)

    def test_same_vote_id_different_payload_rejected(self):
        self.to_voting()
        self.vote(A, "vote-1", choice="approve")
        with self.assertRaises(DuplicateVoteError):
            self.vote(A, "vote-1", choice="reject")

    def test_latest_vote_supersedes(self):
        self.to_voting()
        self.vote(A, "vote-1", choice="approve")
        self.vote(A, "vote-2", choice="reject")
        self.vote(C, "vote-3", choice="approve")
        sealed = self.seal()
        self.assertEqual([v["vote_id"] for v in sealed.valid_votes], ["vote-2", "vote-3"])
        reasons = {e["vote_id"]: e["reason"] for e in sealed.excluded_votes}
        self.assertEqual(reasons, {"vote-1": "superseded"})

    def test_withdrawal_only_before_seal(self):
        self.to_voting()
        self.vote(A, "vote-1")
        self.vote(C, "vote-3")
        self.service.withdraw_vote(actor=A, case_id=CASE_ID, vote_id="vote-1", now=at(T["voting"]))
        sealed = self.seal()
        self.assertEqual([v["vote_id"] for v in sealed.valid_votes], ["vote-3"])
        with self.assertRaises(CaseSealedError):
            self.service.withdraw_vote(actor=C, case_id=CASE_ID, vote_id="vote-3", now=at(T["unblind"]))

    def test_recovery_preserves_sealed_result(self):
        self.to_voting()
        self.vote(A, "vote-1", choice="approve_with_conditions")
        self.vote(C, "vote-3", choice="approve")
        sealed = self.seal()
        resolution = self.service.publish_resolution(actor=SECRETARY, case_id=CASE_ID, now=at(T["unblind"]))

        recovered = BlindReviewService.load(self.log_path, self.contract, self.registry)
        restored = recovered.get_case(CASE_ID)
        self.assertEqual(restored.sealed.seal_hash, sealed.seal_hash)
        self.assertEqual(restored.sealed.valid_votes, sealed.valid_votes)
        self.assertEqual(
            recovered.publish_resolution(actor=SECRETARY, case_id=CASE_ID, now=at(T["unblind"])),
            resolution,
        )
        with self.assertRaises(CaseSealedError):
            recovered.cast_vote(actor=C, case_id=CASE_ID, vote_id="vote-9",
                                choice="reject", now=at(T["unblind"]))


class TestWindows(ServiceFixture):
    def test_vote_outside_window_rejected(self):
        self.to_voting()
        with self.assertRaises(WindowError):
            self.vote(A, "vote-1", moment="2026-09-14T10:00:00+08:00")  # 窗口未开
        with self.assertRaises(WindowError):
            self.vote(A, "vote-1", moment="2026-09-16T18:00:00+08:00")  # 窗口已关

    def test_seal_before_window_close_rejected(self):
        self.to_voting()
        with self.assertRaises(WindowError):
            self.service.seal_case(actor=SECRETARY, case_id=CASE_ID, now=at(T["voting"]))

    def test_advance_before_window_start_rejected(self):
        self.register()
        with self.assertRaises(WindowError):
            self.service.advance_phase(actor=SECRETARY, case_id=CASE_ID, now=at("2026-09-03T09:00:00+08:00"))

    def test_inquiry_window_enforced(self):
        self.register()
        self.disclose_all()
        self.open_review()
        with self.assertRaises(CaseStateError):
            self.service.raise_inquiry(
                actor=A, case_id=CASE_ID, inquiry_id="q-1", text="太早了", now=at(T["review"]),
            )


class TestIdentityDisclosure(ServiceFixture):
    def test_sensitivity_levels_control_resolution(self):
        for sensitivity, after_unblind_allowed in (("standard", True), ("restricted", False)):
            with self.subTest(sensitivity=sensitivity):
                self.tmp2 = tempfile.TemporaryDirectory()
                service = BlindReviewService(
                    EventStore(Path(self.tmp2.name) / "events.jsonl"), self.contract, self.registry
                )
                service.register_case(
                    actor=SECRETARY, case_id=CASE_ID, sensitivity=sensitivity,
                    calendar=CaseCalendar.from_dict(CALENDAR), author_alias="author-1",
                    reviewer_aliases=["reviewer-a", "reviewer-b", "reviewer-c"],
                    sections=SECTIONS, quorum=2, secretary_token=TOKEN, now=at(T["register"]),
                )
                with self.assertRaises(IdentitySealError):
                    service.resolve_identity(actor=A, case_id=CASE_ID, alias="reviewer-b")
                for actor in (A, B, C):
                    service.submit_disclosure(actor=actor, case_id=CASE_ID, statement="无",
                                              has_conflict=False, now=at(T["disclose"]))
                service.advance_phase(actor=SECRETARY, case_id=CASE_ID, now=at(T["review_open"]))
                service.advance_phase(actor=SECRETARY, case_id=CASE_ID, now=at(T["questions_open"]))
                service.advance_phase(actor=SECRETARY, case_id=CASE_ID, now=at(T["voting_open"]))
                service.cast_vote(actor=A, case_id=CASE_ID, vote_id="vote-1", choice="approve", now=at(T["voting"]))
                service.cast_vote(actor=C, case_id=CASE_ID, vote_id="vote-3", choice="approve", now=at(T["voting"]))
                service.seal_case(actor=SECRETARY, case_id=CASE_ID, now=at(T["seal"]))
                service.unblind_case(actor=SECRETARY, case_id=CASE_ID, token=TOKEN, now=at(T["unblind"]))
                if after_unblind_allowed:
                    identity = service.resolve_identity(actor=A, case_id=CASE_ID, alias="reviewer-b")
                    self.assertEqual(identity.real_name, "陈默")
                else:
                    with self.assertRaises(IdentitySealError):
                        service.resolve_identity(actor=A, case_id=CASE_ID, alias="reviewer-b")
                    chair_view = service.resolve_identity(actor=CHAIR, case_id=CASE_ID, alias="reviewer-b")
                    self.assertEqual(chair_view.real_name, "陈默")
                self.tmp2.cleanup()

    def test_maximum_level_never_resolves(self):
        self.register(sensitivity="maximum")
        self.disclose_all()
        self.open_review()
        self.open_questions()
        self.open_voting()
        self.vote(A, "vote-1")
        self.vote(C, "vote-3")
        self.seal()
        self.service.unblind_case(actor=SECRETARY, case_id=CASE_ID, token=TOKEN, now=at(T["unblind"]))
        with self.assertRaises(IdentitySealError):
            self.service.resolve_identity(actor=SECRETARY, case_id=CASE_ID, alias="reviewer-b")


class TestOpinionVisibility(ServiceFixture):
    def test_blind_period_visibility(self):
        self.register()
        self.disclose_all()
        self.open_review()
        self.service.submit_opinion(
            actor=A, case_id=CASE_ID, opinion_id="op-1", sections=["cleaning"],
            recommendation="approve", text="可行", now=at(T["review"]),
        )
        own = self.service.list_opinions(actor=A, case_id=CASE_ID)
        self.assertEqual(len(own), 1)
        self.assertEqual(self.service.list_opinions(actor=C, case_id=CASE_ID), [])  # 他人不可见
        with self.assertRaises(AccessDeniedError):
            self.service.list_opinions(actor=AUTHOR, case_id=CASE_ID)  # 作者解盲前不可见
        self.assertEqual(len(self.service.list_opinions(actor=SECRETARY, case_id=CASE_ID)), 1)


if __name__ == "__main__":
    unittest.main()
