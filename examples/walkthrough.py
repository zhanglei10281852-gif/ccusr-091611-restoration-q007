"""端到端演示：争议造像修复方案的盲审全流程。

对应场景：秘书汇总意见时发现某外部专家与材料供应方存在合作，
其意见已影响其他评审人 —— 于是回避该专家、保留旧意见但不计票，
关键段落修改只重开受影响评审，迟到意见记为旁注，最终封存、
解盲并发布可核对的决议。

运行：python examples/walkthrough.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from blind_review import (  # noqa: E402
    AccessDeniedError,
    Actor,
    AuthorizationError,
    BlindReviewService,
    CaseCalendar,
    CaseSealedError,
    EventStore,
    Identity,
    IdentityLeakError,
    IdentityRegistry,
    IdentitySealError,
    load_contract,
    parse_dt,
)

SECRETARY = Actor(role="secretary", alias="secretary-1")
CHAIR = Actor(role="committee_chair", alias="chair-1")
AUTHOR = Actor(role="proposal_author", alias="author-1")
A = Actor(role="reviewer", alias="reviewer-a")
B = Actor(role="reviewer", alias="reviewer-b")
C = Actor(role="reviewer", alias="reviewer-c")

SECTIONS_V1 = [
    {"section_id": "condition-report", "title": "保存状况", "content": "造像表面鎏金剥落", "key": False},
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


def at(moment: str):
    return parse_dt(moment)


def main() -> None:
    contract = load_contract()
    registry = IdentityRegistry()
    registry.register(Identity(alias="reviewer-a", real_name="王岚", organization="本院修复部"))
    registry.register(Identity(alias="reviewer-b", real_name="陈默", organization="外部材料顾问"))
    registry.register(Identity(alias="reviewer-c", real_name="李青", organization="高校文保实验室"))
    registry.register(Identity(alias="author-1", real_name="赵远", organization="修复工作室"))

    log_path = Path(tempfile.mkdtemp(prefix="blind-review-")) / "events.jsonl"
    service = BlindReviewService(EventStore(log_path), contract, registry)

    print("== 1. 立案与利益冲突申报 ==")
    service.register_case(
        actor=SECRETARY,
        case_id="case-2026-31",
        sensitivity="restricted",
        calendar=CaseCalendar.from_dict(CALENDAR),
        author_alias="author-1",
        reviewer_aliases=["reviewer-a", "reviewer-b", "reviewer-c"],
        sections=SECTIONS_V1,
        quorum=2,
        secretary_token="sec-token-2026",
        now=at("2026-09-01T09:00:00+08:00"),
    )
    service.submit_disclosure(actor=A, case_id="case-2026-31", statement="无利益关系", has_conflict=False, now=at("2026-09-01T10:00:00+08:00"))
    service.submit_disclosure(actor=B, case_id="case-2026-31", statement="无利益关系", has_conflict=False, now=at("2026-09-01T10:05:00+08:00"))

    try:
        service.get_proposal_content(actor=C, case_id="case-2026-31")
        raise AssertionError("未申报者不应看到方案")
    except AccessDeniedError as exc:
        print(f"  未申报的 reviewer-c 接触方案被拒绝：{exc}")
    service.submit_disclosure(actor=C, case_id="case-2026-31", statement="无利益关系", has_conflict=False, now=at("2026-09-02T09:00:00+08:00"))
    print("  reviewer-c 完成申报")

    print("== 2. 独立意见与回避 ==")
    service.advance_phase(actor=SECRETARY, case_id="case-2026-31", now=at("2026-09-06T09:00:00+08:00"))
    service.get_proposal_content(actor=C, case_id="case-2026-31")
    print("  评审开放后，已申报的 reviewer-c 可阅读方案")
    service.submit_opinion(actor=A, case_id="case-2026-31", opinion_id="op-1", sections=["cleaning", "infill"], recommendation="approve_with_conditions", text="清洗方案需降低酸度", now=at("2026-09-08T11:00:00+08:00"))
    service.submit_opinion(actor=B, case_id="case-2026-31", opinion_id="op-2", sections=["infill"], recommendation="approve", text="建议采用供应方X的填料", now=at("2026-09-09T15:00:00+08:00"))

    # 秘书汇总时才得知 reviewer-b 与材料供应方合作 → 回避；旧意见保留但不计票
    service.recuse_reviewer(actor=CHAIR, case_id="case-2026-31", alias="reviewer-b", reason="与材料供应方存在合作关系", now=at("2026-09-10T10:00:00+08:00"))
    case = service.get_case("case-2026-31")
    assert case.opinions["op-2"].state == "excluded"
    print("  reviewer-b 被回避：op-2 保留为 excluded，不计入法定人数")

    print("== 3. 关键段落修改：只重开受影响评审 ==")
    sections_v2 = [dict(s) for s in SECTIONS_V1]
    sections_v2[1]["content"] = "激光清洗"  # 关键段落 cleaning 变更
    result = service.amend_proposal(actor=AUTHOR, case_id="case-2026-31", sections=sections_v2, now=at("2026-09-11T09:30:00+08:00"))
    print(f"  版本升至 v{result['version']}，重开意见：{result['reopened_opinions']}")
    assert result["reopened_opinions"] == ["op-1"]  # op-2 已 excluded，不受影响
    service.submit_opinion(actor=A, case_id="case-2026-31", opinion_id="op-1", sections=["cleaning", "infill"], recommendation="approve_with_conditions", text="激光清洗可行，需补充老化测试", now=at("2026-09-11T16:00:00+08:00"))
    service.attach_evidence(actor=AUTHOR, case_id="case-2026-31", attachment={"attachment_id": "ev-1", "title": "激光清洗光谱分析", "uri": "vault://ev-1", "sha256": "ab" * 32}, now=at("2026-09-11T17:00:00+08:00"))

    print("== 4. 质询、迟到旁注与提醒 ==")
    service.advance_phase(actor=SECRETARY, case_id="case-2026-31", now=at("2026-09-13T09:00:00+08:00"))
    service.raise_inquiry(actor=A, case_id="case-2026-31", inquiry_id="q-1", text="激光清洗对鎏金层的热影响数据？", now=at("2026-09-13T10:00:00+08:00"))
    service.answer_inquiry(actor=AUTHOR, case_id="case-2026-31", inquiry_id="q-1", text="见附件 ev-1 第 3 节", now=at("2026-09-13T14:00:00+08:00"))
    state = service.submit_opinion(actor=C, case_id="case-2026-31", opinion_id="op-3", sections=["monitoring"], recommendation="reject", text="监测周期过短", now=at("2026-09-13T16:00:00+08:00"))
    assert state == "late_note"
    print("  reviewer-c 的迟到意见已记为旁注（late_note），不计票")

    first = service.remind(case_id="case-2026-31", event_type="questions_reminder", recipient_alias="reviewer-c", now=at("2026-09-13T18:00:00+08:00"))
    retry = service.remind(case_id="case-2026-31", event_type="questions_reminder", recipient_alias="reviewer-c", now=at("2026-09-14T09:00:00+08:00"))
    assert first.event_id == retry.event_id and retry.attempt == 2
    print(f"  提醒重试幂等：event_id={retry.event_id} attempt={retry.attempt}")
    try:
        service.notifier.send_reminder(case_id="case-2026-31", event_type="review_reminder", recipient_alias="reviewer-c", now=at("2026-09-14T09:30:00+08:00"), body_template="提醒：李青老师请尽快")
        raise AssertionError("含真实姓名的通知不应通过")
    except IdentityLeakError as exc:
        print(f"  含真实姓名的通知被拦截：{exc}")

    print("== 5. 投票、封存与解盲 ==")
    service.advance_phase(actor=SECRETARY, case_id="case-2026-31", now=at("2026-09-15T09:00:00+08:00"))
    service.cast_vote(actor=A, case_id="case-2026-31", vote_id="vote-1", choice="approve", now=at("2026-09-15T10:00:00+08:00"))
    service.cast_vote(actor=A, case_id="case-2026-31", vote_id="vote-2", choice="approve_with_conditions", now=at("2026-09-15T10:30:00+08:00"))  # 改票：vote-1 被取代
    again = service.cast_vote(actor=A, case_id="case-2026-31", vote_id="vote-2", choice="approve_with_conditions", now=at("2026-09-15T10:31:00+08:00"))
    assert again.vote_id == "vote-2" and len(service.get_case("case-2026-31").votes) == 2  # 幂等重试
    service.cast_vote(actor=C, case_id="case-2026-31", vote_id="vote-3", choice="approve", now=at("2026-09-15T11:00:00+08:00"))

    sealed = service.seal_case(actor=SECRETARY, case_id="case-2026-31", now=at("2026-09-16T19:00:00+08:00"))
    print(f"  封存：有效票 {len(sealed.valid_votes)}，法定人数达标={sealed.quorum_met}，seal_hash={sealed.seal_hash[:16]}…")

    try:
        service.unblind_case(actor=SECRETARY, case_id="case-2026-31", token="wrong-token", now=at("2026-09-17T09:00:00+08:00"))
        raise AssertionError("错误令牌不应解盲")
    except AuthorizationError as exc:
        print(f"  错误授权令牌被拒绝：{exc}")
    service.unblind_case(actor=SECRETARY, case_id="case-2026-31", token="sec-token-2026", now=at("2026-09-17T09:05:00+08:00"))
    print("  票数达标 + 秘书授权 → 已解盲（不可逆）")

    try:
        service.resolve_identity(actor=A, case_id="case-2026-31", alias="reviewer-b")
        raise AssertionError("restricted 级别下评审不应解析他人身份")
    except IdentitySealError:
        print("  restricted 级别：解盲后评审仍无法解析他人身份，仅秘书/主任委员可解析")

    print("== 6. 决议发布与核对 ==")
    resolution = service.publish_resolution(actor=SECRETARY, case_id="case-2026-31", now=at("2026-09-17T10:00:00+08:00"))
    print(f"  采用方案版本：v{resolution['proposal_version']}  计票：{resolution['tally']}")
    print(f"  法定人数：{resolution['quorum']['valid_votes']}/{resolution['quorum']['required']}")
    print(f"  异议链：{[(e['kind'], e['ref'], e['counted']) for e in resolution['dissent_chain']]}")
    print(f"  被回避专家意见（保留备查）：{resolution['excluded_opinions']}")
    assert service.verify_resolution(case_id="case-2026-31", resolution=resolution)
    tampered = dict(resolution, tally={"approve": 2, "approve_with_conditions": 0, "reject": 0, "abstain": 0})
    assert not service.verify_resolution(case_id="case-2026-31", resolution=tampered)
    print("  决议核对通过；篡改计票后核对失败")

    print("== 7. 崩溃恢复：封存结果不变 ==")
    recovered = BlindReviewService.load(log_path, contract, registry)
    assert recovered.get_case("case-2026-31").sealed.seal_hash == sealed.seal_hash
    assert recovered.publish_resolution(actor=SECRETARY, case_id="case-2026-31", now=at("2026-09-17T11:00:00+08:00")) == resolution
    try:
        recovered.cast_vote(actor=C, case_id="case-2026-31", vote_id="vote-9", choice="reject", now=at("2026-09-17T11:30:00+08:00"))
        raise AssertionError("解盲后不应再接受投票")
    except CaseSealedError as exc:
        print(f"  恢复后封存结果一致；解盲后的投票被拒绝：{exc}")

    print("\n全流程演示完成。")


if __name__ == "__main__":
    main()
