import json
from datetime import datetime
from pathlib import Path

root = Path(__file__).resolve().parents[1]
contract = json.loads((root / "domain" / "contract.json").read_text(encoding="utf-8"))
case = json.loads((root / "examples" / "review_case.json").read_text(encoding="utf-8"))
aliases = {item["alias"]: item["conflict_state"] for item in case["reviewers"]}
required = set(contract["required_vote_fields"])
assert case["state"] in contract["case_states"]
assert case["sensitivity"] in contract["sensitivity_levels"]
assert all(state in contract["conflict_states"] for state in aliases.values())
assert all(required <= set(vote) for vote in case["votes"])
assert all(vote["reviewer_alias"] in aliases for vote in case["votes"])
assert all(aliases[vote["reviewer_alias"]] == "clear" for vote in case["votes"])
assert all(vote["choice"] in contract["vote_choices"] for vote in case["votes"])

# 截止日历：窗口名称合法、时间可解析且开启早于关闭
calendar = json.loads((root / "domain" / "deadline_calendar.json").read_text(encoding="utf-8"))
for case_id, entry in calendar.get("cases", {}).items():
    assert set(entry["windows"]) <= set(contract["windows"]), case_id
    for window in entry["windows"].values():
        assert datetime.fromisoformat(window["opens_at"]) < datetime.fromisoformat(window["closes_at"]), case_id

# 通知事件示例：事件类型合法，且不含任何身份字段
notifications = json.loads((root / "domain" / "notification_events.json").read_text(encoding="utf-8"))
for event in notifications["examples"]:
    assert event["event"] in contract["notification_events"], event
    assert not (set(event) & set(contract["identity_forbidden_fields"])), event

print("盲审契约与案件样例格式有效")
