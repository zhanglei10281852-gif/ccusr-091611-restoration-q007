import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
contract = json.loads((root / "domain" / "contract.json").read_text(encoding="utf-8"))
case = json.loads((root / "examples" / "review_case.json").read_text(encoding="utf-8"))
aliases = {item["alias"]: item["conflict_state"] for item in case["reviewers"]}
required = set(contract["required_vote_fields"])
assert case["state"] in contract["case_states"]
assert all(state in contract["conflict_states"] for state in aliases.values())
assert all(required <= set(vote) for vote in case["votes"])
assert all(vote["reviewer_alias"] in aliases for vote in case["votes"])
assert all(aliases[vote["reviewer_alias"]] == "clear" for vote in case["votes"])
print("盲审契约与案件样例格式有效")

