"""别名与真实身份的绑定库，与评审内容分开保存。

披露策略按案件敏感级别控制：例如 restricted 案件在解盲前只有秘书
可以解析真实身份，解盲后才扩大到主任委员。评审流程与通知中只出现别名。
"""

from __future__ import annotations

from .errors import AccessDenied, InvalidState


class IdentityVault:
    def __init__(self, disclosure_policy: dict):
        self.policy = disclosure_policy
        self._bindings: dict[tuple[str, str], dict] = {}

    def bind(self, case_id: str, alias: str, identity: dict) -> None:
        self._bindings[(case_id, alias)] = dict(identity)

    def resolve(self, case_id: str, alias: str, *, requester_role: str, sensitivity: str, unblinded: bool) -> dict:
        phase = "after_unblind" if unblinded else "before_unblind"
        allowed = self.policy[sensitivity][phase]
        if requester_role not in allowed:
            raise AccessDenied(f"敏感级别 {sensitivity} 下 {requester_role} 在此阶段无权查看真实身份")
        try:
            return dict(self._bindings[(case_id, alias)])
        except KeyError:
            raise InvalidState(f"无此身份绑定: {case_id}/{alias}") from None

    def identity_strings(self, case_id: str) -> list[str]:
        """该案件所有真实身份字段值，用于扫描通知是否泄露身份。"""
        return [
            str(value)
            for (cid, _alias), identity in self._bindings.items()
            if cid == case_id
            for value in identity.values()
        ]
