# 修复方案盲审与利益冲突管理

项目资料定义方案盲审中的身份范围、利益冲突申报、独立意见和决议封存。评审内容与真实身份分开表达，通知仅使用不泄露身份的案件引用。

`domain/contract.json` 记录角色与阶段，`examples/review_case.json` 是一份未解盲案件。运行 `python tools/validate_contract.py` 可检查评审引用、回避状态与票数结构。

