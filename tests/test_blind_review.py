"""盲审引擎的八条核心保证：

1. 未完成冲突申报的人不能接触方案内容；
2. 被回避专家的旧意见保留但不计入法定人数；
3. 关键段落修改只重开受影响的评审；
4. 解盲需法定票数 + 秘书授权且不可逆；
5. 提醒重试不泄露作者或评审身份；
6. 迟到意见保存为旁注而非悄悄计票；
7. 决议携带可核对的有效票数、异议链与采用的确切方案版本；
8. 并发投票、撤回与系统恢复不能改变已封存的结果。
"""

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blind_review import (
    AccessDenied,
    AuthorizationError,
    BlindReviewEngine,
    CaseSealedError,
    EventStore,
    IdentityLeakError,
    IdentityVault,
    InvalidState,
    QuorumNotMet,
    WindowClosed,
    assert_identity_safe,
    build_windows,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / "domain" / "contract.json").read_text(encoding="utf-8"))
TOKEN = "secretary-token-2026"
TZ = timezone(timedelta(hours=8))

REVIEWERS = ("reviewer-a", "reviewer-b", "reviewer-c")
SECTIONS = [
    {"section_id": "sec-1", "title": "清洗方案", "content": "蒸馏水轻柔清洗", "is_key": True},
    {"section_id": "sec-2", "title": "补配材料", "content": "矿物颜料补色", "is_key": True},
    {"section_id": "sec-3", "title": "记录格式", "content": "按馆藏规范拍照存档", "is_key": False},
]


def ts(day, hour=10):
    return datetime(2026, 9, day, hour, tzinfo=TZ).isoformat()


WINDOWS = {
    "disclosure": {"opens_at": ts(1, 9), "closes_at": ts(6, 18)},
    "review": {"opens_at": ts(6, 18), "closes_at": ts(16, 18)},
    "questions": {"opens_at": ts(16, 18), "closes_at": ts(21, 18)},
    "voting": {"opens_at": ts(21, 18), "closes_at": ts(26, 18)},
}


def make_engine(tmp_path, sensitivity="restricted", token=TOKEN):
    store = EventStore(tmp_path / "events.jsonl")
    vault = IdentityVault(CONTRACT["identity_disclosure"])
    engine = BlindReviewEngine(store, CONTRACT, vault=vault, secretary_token=token)
    return engine, vault


def boot_case(engine, case_id="case-1", sensitivity="restricted", declare=True):
    engine.open_case(case_id, sensitivity, WINDOWS, at=ts(1, 9))
    engine.register_participant(case_id, "author", "proposal_author")
    engine.register_participant(case_id, "chair", "committee_chair")
    for alias in REVIEWERS:
        engine.register_participant(case_id, alias, "reviewer")
    engine.submit_proposal(case_id, "author", SECTIONS, [{"attachment_id": "att-1", "kind": "xray", "uri": "vault://att-1"}], at=ts(2))
    if declare:
        for alias in REVIEWERS:
            engine.submit_disclosure(case_id, alias, has_conflict=False, at=ts(3))
    return case_id


def to_voting(engine, case_id="case-1"):
    engine.open_review(case_id, at=ts(6, 19))
    engine.open_questions(case_id, at=ts(16, 19))
    engine.open_voting(case_id, at=ts(21, 19))


def cast_and_seal(engine, case_id, votes):
    for alias, choice in votes:
        engine.cast_vote(case_id, alias, choice, at=ts(25))
    return engine.seal(case_id, at=ts(27))


# ---------------------------------------------------------------- 1. 申报门禁


def test_undeclared_reviewer_cannot_access_proposal(tmp_path):
    engine, _ = make_engine(tmp_path)
    case_id = boot_case(engine, declare=False)

    with pytest.raises(AccessDenied):
        engine.get_proposal(case_id, "reviewer-a")  # 未申报

    engine.submit_disclosure(case_id, "reviewer-a", has_conflict=True, at=ts(3))
    with pytest.raises(AccessDenied):
        engine.get_proposal(case_id, "reviewer-a")  # 申报待裁定

    engine.decide_conflict(case_id, "reviewer-a", "clear", by_role="committee_chair", at=ts(4))
    proposal = engine.get_proposal(case_id, "reviewer-a")
    assert proposal["version"] == 1

    engine.decide_conflict(case_id, "reviewer-a", "recused", by_role="committee_chair", at=ts(5))
    with pytest.raises(AccessDenied):
        engine.get_proposal(case_id, "reviewer-a")  # 已回避

    # 作者与秘书不受申报门禁限制
    assert engine.get_proposal(case_id, "author")["version"] == 1


# ---------------------------------------------------------------- 2. 回避保留但不计票


def test_recused_expert_opinions_retained_but_not_counted(tmp_path):
    engine, _ = make_engine(tmp_path)
    case_id = boot_case(engine)
    engine.open_review(case_id, at=ts(6, 19))

    opinion_id, state = engine.submit_opinion(case_id, "reviewer-b", ["sec-1"], "object", "清洗方案风险过高", at=ts(10))
    assert state == "submitted"

    # 事后发现该外部专家与材料供应方存在合作：裁定回避
    engine.decide_conflict(case_id, "reviewer-b", "recused", by_role="committee_chair", at=ts(12))

    opinion = engine.cases[case_id]["opinions"][opinion_id]
    assert opinion["state"] == "excluded"  # 旧意见保留
    assert opinion["text"] == "清洗方案风险过高"
    with pytest.raises(AccessDenied):
        engine.get_proposal(case_id, "reviewer-b")

    engine.open_questions(case_id, at=ts(16, 19))
    engine.open_voting(case_id, at=ts(21, 19))
    with pytest.raises(AccessDenied):
        engine.cast_vote(case_id, "reviewer-b", "reject", at=ts(25))

    seal = cast_and_seal(engine, case_id, [("reviewer-a", "approve"), ("reviewer-c", "approve")])
    assert seal["eligible"] == ["reviewer-a", "reviewer-c"]  # 回避者退出法定人数
    assert seal["quorum_met"]

    resolution = engine.publish_resolution(case_id, TOKEN, at=ts(28))
    assert resolution["valid_votes"] == 2
    assert resolution["excluded_opinion_ids"] == [opinion_id]
    assert all(entry["alias"] != "reviewer-b" for entry in resolution["dissent_chain"])


# ---------------------------------------------------------------- 3. 局部重开


def amended(sections, section_id, content):
    return [dict(s, content=content) if s["section_id"] == section_id else dict(s) for s in sections]


def test_amendment_reopens_only_affected_reviews(tmp_path):
    engine, _ = make_engine(tmp_path)
    case_id = boot_case(engine)
    engine.open_review(case_id, at=ts(6, 19))

    op_a, _ = engine.submit_opinion(case_id, "reviewer-a", ["sec-1"], "support", "清洗可行", at=ts(10))
    op_b, _ = engine.submit_opinion(case_id, "reviewer-b", ["sec-2"], "support", "补色可行", at=ts(10, 11))
    op_c, _ = engine.submit_opinion(case_id, "reviewer-c", ["sec-1", "sec-3"], "conditional", "需补充记录", at=ts(11))

    # 只改非关键段落：不重开任何评审
    version = engine.submit_proposal(case_id, "author", amended(SECTIONS, "sec-3", "按新规范拍照存档"), [], at=ts(12))
    opinions = engine.cases[case_id]["opinions"]
    assert version == 2
    assert all(o["state"] == "submitted" for o in opinions.values())
    assert all(o["proposal_version"] == 2 for o in opinions.values())

    # 修改关键段落 sec-1：只重开引用 sec-1 的评审
    current = engine.get_proposal(case_id, "author")["sections"]
    version = engine.submit_proposal(case_id, "author", amended(current, "sec-1", "激光清洗"), [], at=ts(13))
    assert version == 3
    assert opinions[op_a]["state"] == "draft" and opinions[op_a]["reopened_due_to"] == ["sec-1"]
    assert opinions[op_c]["state"] == "draft"  # 引用了 sec-1
    assert opinions[op_b]["state"] == "submitted" and opinions[op_b]["proposal_version"] == 3

    # 被重开的评审针对新版本重新提交
    _, state = engine.submit_opinion(case_id, "reviewer-a", ["sec-1"], "conditional", "激光清洗需限定功率", at=ts(14), opinion_id=op_a)
    assert state == "submitted"
    assert opinions[op_a]["proposal_version"] == 3


# ---------------------------------------------------------------- 4. 解盲门槛与不可逆


def test_unblind_requires_quorum_and_secretary_and_is_irreversible(tmp_path):
    # 票数不足：封存后既不能解盲也不能发布决议
    engine, _ = make_engine(tmp_path / "low")
    case_id = boot_case(engine)
    to_voting(engine, case_id)
    seal = cast_and_seal(engine, case_id, [("reviewer-a", "approve")])
    assert not seal["quorum_met"]
    with pytest.raises(QuorumNotMet):
        engine.unblind(case_id, TOKEN, at=ts(28))
    with pytest.raises(QuorumNotMet):
        engine.publish_resolution(case_id, TOKEN, at=ts(28))

    # 票数足够：仍需秘书授权；解盲不可逆
    engine, _ = make_engine(tmp_path / "full")
    case_id = boot_case(engine)
    to_voting(engine, case_id)
    cast_and_seal(engine, case_id, [("reviewer-a", "approve"), ("reviewer-b", "approve"), ("reviewer-c", "reject")])

    with pytest.raises(InvalidState):
        engine.seal(case_id, at=ts(28))  # 重复封存
    with pytest.raises(AuthorizationError):
        engine.unblind(case_id, "forged-token", at=ts(28))

    engine.unblind(case_id, TOKEN, at=ts(28))
    assert engine.cases[case_id]["state"] == "unblinded"
    with pytest.raises(InvalidState):
        engine.unblind(case_id, TOKEN, at=ts(29))  # 不可重复解盲
    with pytest.raises(CaseSealedError):
        engine.submit_opinion(case_id, "reviewer-a", ["sec-1"], "support", "事后补充", at=ts(29))


# ---------------------------------------------------------------- 5. 提醒不泄露身份


def test_reminders_never_leak_identity(tmp_path):
    engine, vault = make_engine(tmp_path)
    case_id = boot_case(engine, declare=False)
    vault.bind(case_id, "reviewer-a", {"real_name": "张伟", "email": "zhangwei@supplier.example"})
    vault.bind(case_id, "author", {"real_name": "李敏", "email": "limin@museum.example"})

    engine.submit_disclosure(case_id, "reviewer-a", has_conflict=False, at=ts(3))
    engine.open_review(case_id, at=ts(6, 19))

    for retry in range(4):  # 任意重试次数都不泄露
        events = engine.pending_reminders(case_id, at=ts(8), retry=retry)
        assert events  # 存在未申报提醒与未交意见提醒
        blob = json.dumps(events, ensure_ascii=False)
        assert "张伟" not in blob and "李敏" not in blob
        assert "zhangwei@supplier.example" not in blob
        for event in events:
            assert_identity_safe(event, CONTRACT["identity_forbidden_fields"], vault.identity_strings(case_id))

    with pytest.raises(IdentityLeakError):
        assert_identity_safe({"event": "review_reminder", "real_name": "张伟"}, CONTRACT["identity_forbidden_fields"], [])


def test_identity_disclosure_follows_sensitivity(tmp_path):
    engine, vault = make_engine(tmp_path)
    case_id = boot_case(engine)
    vault.bind(case_id, "reviewer-a", {"real_name": "张伟"})

    # restricted：解盲前仅秘书可见
    with pytest.raises(AccessDenied):
        engine.resolve_identity(case_id, "reviewer-a", requester_role="committee_chair")
    assert engine.resolve_identity(case_id, "reviewer-a", requester_role="secretary")["real_name"] == "张伟"

    to_voting(engine, case_id)
    cast_and_seal(engine, case_id, [("reviewer-a", "approve"), ("reviewer-b", "approve"), ("reviewer-c", "approve")])
    engine.unblind(case_id, TOKEN, at=ts(28))
    assert engine.resolve_identity(case_id, "reviewer-a", requester_role="committee_chair")["real_name"] == "张伟"
    with pytest.raises(AccessDenied):
        engine.resolve_identity(case_id, "reviewer-a", requester_role="reviewer")

    # internal：解盲前主任委员即可见
    engine2, vault2 = make_engine(tmp_path / "internal")
    case2 = boot_case(engine2, case_id="case-2", sensitivity="internal")
    vault2.bind(case2, "reviewer-a", {"real_name": "张伟"})
    assert engine2.resolve_identity(case2, "reviewer-a", requester_role="committee_chair")["real_name"] == "张伟"


# ---------------------------------------------------------------- 6. 迟到意见成为旁注


def test_late_opinion_saved_as_side_note_not_counted(tmp_path):
    engine, _ = make_engine(tmp_path)
    case_id = boot_case(engine)
    engine.open_review(case_id, at=ts(6, 19))
    engine.open_questions(case_id, at=ts(16, 19))

    opinion_id, state = engine.submit_opinion(case_id, "reviewer-a", ["sec-2"], "object", "补色材料存疑", at=ts(20))
    assert state == "late_note"  # 评审窗口已关闭：保存为旁注

    engine.open_voting(case_id, at=ts(21, 19))
    cast_and_seal(engine, case_id, [("reviewer-a", "approve"), ("reviewer-b", "approve"), ("reviewer-c", "approve")])
    resolution = engine.publish_resolution(case_id, TOKEN, at=ts(28))

    assert resolution["late_note_ids"] == [opinion_id]  # 旁注可见
    assert resolution["valid_votes"] == 3  # 但绝不影响票数
    assert resolution["dissent_chain"] == []  # 也不进入异议链


# ---------------------------------------------------------------- 7. 决议可核对


def test_resolution_is_verifiable(tmp_path):
    engine, _ = make_engine(tmp_path)
    case_id = boot_case(engine)
    engine.open_review(case_id, at=ts(6, 19))
    op_c, _ = engine.submit_opinion(case_id, "reviewer-c", ["sec-1"], "object", "清洗方案不可接受", at=ts(10))
    engine.open_questions(case_id, at=ts(16, 19))
    inquiry_id, events = engine.submit_inquiry(case_id, "reviewer-c", ["sec-1"], "蒸馏水残留如何控制？", at=ts(18))
    assert events[0]["event"] == "question_routed" and events[0]["alias"] == "author"
    engine.answer_inquiry(case_id, "author", inquiry_id, "已在附件 att-1 给出干燥曲线", at=ts(19))
    engine.open_voting(case_id, at=ts(21, 19))

    seal = cast_and_seal(
        engine,
        case_id,
        [("reviewer-a", "approve"), ("reviewer-b", "approve_with_conditions"), ("reviewer-c", "reject")],
    )
    resolution = engine.publish_resolution(case_id, TOKEN, at=ts(28))

    # 可核对的有效票数
    assert resolution["valid_votes"] == 3
    assert resolution["valid_vote_ids"] == ["vote-1", "vote-2", "vote-3"]
    assert resolution["tally"] == {"approve": 1, "approve_with_conditions": 1, "reject": 1}
    assert resolution["quorum"] == {"required": 2, "met": True, "eligible": 3}
    # 采用的确切方案版本与决议结果
    assert resolution["proposal_version"] == seal["proposal_version"] == 1
    assert resolution["decision"] == "adopted_with_conditions"
    # 异议链：反对意见 + 反对票，按时间排序且哈希链接
    chain = resolution["dissent_chain"]
    assert [entry["kind"] for entry in chain] == ["opinion", "vote"]
    assert chain[0]["ref"] == op_c and chain[0]["alias"] == "reviewer-c"
    assert chain[1]["prev_digest"] == chain[0]["digest"]
    # 从日志重算可验证
    assert engine.verify_resolution(case_id)


# ---------------------------------------------------------------- 8. 并发、撤回与恢复


def test_concurrent_votes_withdrawal_and_recovery_preserve_sealed(tmp_path):
    engine, vault = make_engine(tmp_path)
    case_id = boot_case(engine)
    to_voting(engine, case_id)

    # 并发投票：不同评审同时投票，同一评审同时改票
    def vote(alias, choice):
        engine.cast_vote(case_id, alias, choice, at=ts(25))

    threads = [
        threading.Thread(target=vote, args=("reviewer-a", "approve")),
        threading.Thread(target=vote, args=("reviewer-a", "approve_with_conditions")),
        threading.Thread(target=vote, args=("reviewer-b", "approve")),
        threading.Thread(target=vote, args=("reviewer-c", "reject")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    vote_events = [e for e in engine.store.events if e["type"] == "vote_cast"]
    assert len({e["payload"]["vote"]["vote_id"] for e in vote_events}) == 4  # 无重复票号
    ballots = engine.cases[case_id]["ballots"]
    assert set(ballots) == set(REVIEWERS)
    assert ballots["reviewer-a"]["choice"] in ("approve", "approve_with_conditions")

    # 窗口内撤回一票
    retract_id = ballots["reviewer-c"]["vote_id"]
    engine.retract_vote(case_id, "reviewer-c", retract_id, at=ts(25, 12))
    seal = engine.seal(case_id, at=ts(27))
    assert seal["valid_votes"] == 2 and seal["quorum_met"]

    resolution = engine.publish_resolution(case_id, TOKEN, at=ts(28))
    assert resolution["decision"] in ("adopted", "adopted_with_conditions")  # 取决于 reviewer-a 改票的落点

    # 封存后：投票、撤回、改稿、迟到票一律被拒绝
    with pytest.raises(CaseSealedError):
        engine.cast_vote(case_id, "reviewer-c", "approve", at=ts(28))
    with pytest.raises(CaseSealedError):
        engine.retract_vote(case_id, "reviewer-b", ballots["reviewer-b"]["vote_id"], at=ts(28))
    with pytest.raises(CaseSealedError):
        engine.submit_proposal(case_id, "author", amended(SECTIONS, "sec-1", "激光清洗"), [], at=ts(28))
    with pytest.raises(CaseSealedError):
        engine.withdraw_opinion(case_id, "reviewer-a", "opinion-1", at=ts(28))

    # 系统恢复：从同一日志重建，封存结果分毫不差
    recovered = BlindReviewEngine(EventStore(tmp_path / "events.jsonl"), CONTRACT, vault=vault, secretary_token=TOKEN)
    assert recovered.get_resolution(case_id)["resolution_hash"] == resolution["resolution_hash"]
    assert recovered.verify_resolution(case_id)
    with pytest.raises(CaseSealedError):
        recovered.cast_vote(case_id, "reviewer-c", "approve", at=ts(28))

    recovered.unblind(case_id, TOKEN, at=ts(29))
    again = BlindReviewEngine(EventStore(tmp_path / "events.jsonl"), CONTRACT, vault=vault, secretary_token=TOKEN)
    assert again.cases[case_id]["state"] == "unblinded"
    assert again.get_resolution(case_id)["resolution_hash"] == resolution["resolution_hash"]


# ---------------------------------------------------------------- 窗口与质询边界


def test_inquiry_window_and_late_vote_side_note(tmp_path):
    engine, _ = make_engine(tmp_path)
    case_id = boot_case(engine)
    engine.open_review(case_id, at=ts(6, 19))
    engine.open_questions(case_id, at=ts(16, 19))
    with pytest.raises(WindowClosed):
        engine.submit_inquiry(case_id, "reviewer-a", ["sec-1"], "超时质询", at=ts(22))
    engine.open_voting(case_id, at=ts(21, 19))
    engine.cast_vote(case_id, "reviewer-a", "approve", at=ts(25))
    engine.cast_vote(case_id, "reviewer-b", "approve", at=ts(25))
    # 投票窗口关闭后、封存前的迟到票：保存为旁注，不计票
    vote_id, counted = engine.cast_vote(case_id, "reviewer-c", "reject", at=ts(26, 19))
    assert not counted
    seal = engine.seal(case_id, at=ts(27))
    assert seal["valid_votes"] == 2
    resolution = engine.publish_resolution(case_id, TOKEN, at=ts(28))
    assert resolution["late_vote_ids"] == [vote_id]
    assert resolution["tally"]["reject"] == 0


def test_calendar_builder_matches_contract():
    defaults = json.loads((ROOT / "domain" / "deadline_calendar.json").read_text(encoding="utf-8"))["defaults"]
    windows = build_windows(defaults, "2026-09-01T09:00:00+08:00")
    assert list(windows) == CONTRACT["windows"]
    assert windows["voting"]["opens_at"] == windows["questions"]["closes_at"]
