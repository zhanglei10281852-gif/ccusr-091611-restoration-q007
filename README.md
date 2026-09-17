# 修复方案盲审与利益冲突管理

项目资料定义方案盲审中的身份范围、利益冲突申报、独立意见和决议封存。评审内容与真实身份分开表达，通知仅使用不泄露身份的案件引用。

## 结构

- `domain/contract.json` — 领域契约：案件阶段、角色、冲突状态、意见状态、投票必填字段、敏感级别、阶段窗口、通知事件类型与载荷白名单。
- `blind_review/` — 核心实现（仅标准库）：
  - `service.py` — 阶段流转、申报门控、回避、版本修订重开、投票、封存、解盲、决议发布与核对；一切变更先写事件日志再应用。
  - `calendar.py` — 截止日历与窗口判定；`identity.py` — 别名 ↔ 真实身份注册表与敏感级别披露策略；`notifications.py` — 幂等提醒与泄露拦截；`storage.py` — 追加式事件日志。
- `examples/` — `review_case.json` 未解盲案件样例、`deadline_calendar.json` 截止日历样例、`notification_events.json` 通知事件（含重试）样例、`walkthrough.py` 端到端演示。
- `tests/test_blind_review.py` — 全部领域规则的单元测试。

## 规则摘要

- 未完成冲突申报（undeclared / pending_decision）或已被回避者，禁止接触方案内容。
- 被回避专家的旧意见与旧投票保留备查（excluded），但不计入法定人数。
- 关键段落修改只重开覆盖这些段落的评审；非关键段落修改不重开任何评审。
- 评审窗口关闭后提交的意见记为旁注（late_note），保存但不计票。
- 封存冻结计票快照；解盲需票数达标 + 秘书授权令牌，且不可逆。
- 提醒通知只含案件引用与别名，重试共享 event_id 幂等，发送前强制身份泄露扫描。
- 决议包含有效票数、异议链（正式异议 + 旁注 + 未答复质询）与采用的确切方案版本，可通过 `verify_resolution` 重算核对。
- 并发投票按 vote_id 幂等去重；系统恢复重放事件日志后封存结果逐字节一致。

## 运行

```bash
python tools/validate_contract.py   # 校验契约、案件样例、日历与通知事件
python examples/walkthrough.py      # 端到端演示（含回避、重开、旁注、封存、解盲、恢复）
python -m unittest discover -s tests -v
```

注意：事件日志中包含秘书授权令牌，部署时应限制日志文件权限或改用外部密钥保管。
