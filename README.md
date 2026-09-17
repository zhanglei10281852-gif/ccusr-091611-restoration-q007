# 修复方案盲审与利益冲突管理

项目资料定义方案盲审中的身份范围、利益冲突申报、独立意见和决议封存。评审内容与真实身份分开表达，通知仅使用不泄露身份的案件引用。

## 结构

- `domain/contract.json` — 角色、案件阶段、冲突状态、意见状态、票数规则（法定人数）、解盲条件、敏感级别与身份披露策略。
- `domain/deadline_calendar.json` — 截止日历：申报、评审、质询、投票四个窗口的默认时长与案件实例。
- `domain/notification_events.json` — 通知事件示例：只含案件引用与角色别名。
- `blind_review/` — 盲审引擎：
  - `engine.py` — 状态机与全部评审规则；
  - `store.py` — 只追加事件日志（哈希链 + 写锁），支撑封存不可变与崩溃恢复；
  - `identity.py` — 别名↔真实身份绑定库，按敏感级别控制披露；
  - `notifications.py` — 提醒/路由事件构造与身份泄露扫描；
  - `calendar.py` — 由默认时长生成顺序窗口。
- `examples/review_case.json` — 一份未解盲案件。
- `tests/test_blind_review.py` — 八条核心保证的回归测试。

## 行为保证

1. **申报门禁**：评审角色未完成冲突申报（undeclared / pending_decision）或已被回避时，`get_proposal` 拒绝其接触方案内容。
2. **回避留痕**：被裁定 `recused` 的专家，其旧意见保留为 `excluded`，内容可查但不计入法定人数、不进入异议链；其投票在封存计票时被排除。
3. **局部重开**：方案关键段落（`is_key`）被修改或段落被删除时，只有引用受影响段落的已提交意见回到 `draft` 待重审，其余意见随新版本结转；非关键段落改动不重开任何评审。
4. **解盲门槛**：`unblind` 要求案件已封存、有效票数达到法定人数（默认 ⌈2/3⌉ 且不少于 2）并持有秘书令牌；解盲不可逆，封存/解盲后一切修改抛 `CaseSealedError`。
5. **通知不泄密**：提醒与路由事件只含 `case_ref` 与别名；引擎在发出前递归扫描受禁字段与身份库全部真实身份字符串，任意重试次数都一样安全。
6. **迟到为旁注**：评审窗口关闭后提交的意见保存为 `late_note`，投票窗口关闭后的票保存为迟到票旁注——可见、可审计，但绝不计入票数与异议链。
7. **决议可核对**：`publish_resolution` 输出有效票数、逐选项计票、票号清单、按时间排序且哈希链接的异议链、采用的确切方案版本与封存哈希；`verify_resolution` 可从事件日志重算比对。
8. **封存即定稿**：事件日志只追加且哈希链校验；并发投票在写锁内原子落账，窗口内可撤回/改票，封存后投票、撤回、改稿一律拒绝；系统重启后重放日志，封存结果分毫不差。

## 运行

```bash
python3 tools/validate_contract.py   # 检查评审引用、回避状态、票数结构、日历与通知示例
python3 -m pytest tests/ -q          # 行为保证的回归测试
```

## 最小流程示例

```python
from blind_review import BlindReviewEngine, EventStore, IdentityVault, build_windows
import json

contract = json.load(open("domain/contract.json", encoding="utf-8"))
vault = IdentityVault(contract["identity_disclosure"])
engine = BlindReviewEngine(EventStore("events.jsonl"), contract, vault=vault, secretary_token="…")

windows = build_windows({"disclosure_days": 7, "review_days": 14, "questions_days": 7, "voting_days": 5},
                        "2026-09-01T09:00:00+08:00")
engine.open_case("case-2026-31", "restricted", windows, at="2026-09-01T09:00:00+08:00")
engine.register_participant("case-2026-31", "reviewer-a", "reviewer")
engine.submit_disclosure("case-2026-31", "reviewer-a", has_conflict=False, at="2026-09-02T10:00:00+08:00")
# … 申报 → 开评审 → 意见/质询 → 开投票 → 投票 → 封存 → 解盲/发布决议
```
