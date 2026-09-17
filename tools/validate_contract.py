import json
from datetime import datetime
from pathlib import Path

root = Path(__file__).resolve().parents[1]
contract = json.loads((root / "domain" / "contract.json").read_text(encoding="utf-8"))
case = json.loads((root / "examples" / "review_case.json").read_text(encoding="utf-8"))

# --- 案件样例：评审引用、回避状态与票数结构 ---
aliases = {item["alias"]: item["conflict_state"] for item in case["reviewers"]}
required = set(contract["required_vote_fields"])
assert case["state"] in contract["case_states"]
assert case["sensitivity"] in contract["sensitivity_levels"]
assert all(state in contract["conflict_states"] for state in aliases.values())
assert all(required <= set(vote) for vote in case["votes"])
assert all(vote["reviewer_alias"] in aliases for vote in case["votes"])
# 被回避者不得有计票投票；投票必须对应当前方案版本
assert all(aliases[vote["reviewer_alias"]] == "clear" for vote in case["votes"])
assert all(vote["proposal_version"] == case["proposal_version"] for vote in case["votes"])
assert all(vote["choice"] in contract["vote_choices"] for vote in case["votes"])

# --- 截止日历样例：窗口齐全、首尾合法、阶段顺序衔接 ---
calendar = json.loads((root / "examples" / "deadline_calendar.json").read_text(encoding="utf-8"))
windows = calendar["windows"]
assert set(windows) == set(contract["phase_windows"])
spans = {
    name: (datetime.fromisoformat(span["start"]), datetime.fromisoformat(span["end"]))
    for name, span in windows.items()
}
assert all(start < end for start, end in spans.values())
ordered = [spans[name] for name in contract["phase_windows"]]
assert all(prev[1] <= nxt[0] for prev, nxt in zip(ordered, ordered[1:]))

# --- 通知事件样例：类型已登记、载荷不含身份字段、重试幂等 ---
events = json.loads((root / "examples" / "notification_events.json").read_text(encoding="utf-8"))
allowed_keys = set(contract["notification_payload_allowed_keys"])
allowed_types = set(contract["notification_events"])
for event in events:
    assert set(event) <= allowed_keys, f"通知载荷越界: {set(event) - allowed_keys}"
    assert event["event_type"] in allowed_types
by_id = {}
for event in events:
    prior = by_id.setdefault(event["event_id"], event)
    stable = set(event) - {"attempt", "scheduled_for"}
    assert all(event[k] == prior[k] for k in stable), "重试事件的稳定字段不得变化"

print("盲审契约、案件样例、截止日历与通知事件格式有效")
