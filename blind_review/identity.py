"""身份注册表与按敏感级别的身份披露策略。

评审内容（意见、投票、通知）只携带别名；真实身份单独保存在
IdentityRegistry 中，只有满足敏感级别策略的角色才能解析。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import IdentitySealError

# 解盲前 / 解盲后允许解析真实身份的角色集合。
# standard：解盲后委员会全员可见；
# restricted：解盲后仅秘书与主任委员可见；
# maximum：系统内永不解析，只能走线下授权流程。
_IDENTITY_POLICY = {
    "standard": {
        "before": {"secretary"},
        "after": {"secretary", "committee_chair", "reviewer", "proposal_author", "conservation_scientist"},
    },
    "restricted": {
        "before": {"secretary"},
        "after": {"secretary", "committee_chair"},
    },
    "maximum": {
        "before": set(),
        "after": set(),
    },
}


@dataclass(frozen=True)
class Identity:
    alias: str
    real_name: str
    organization: str = ""
    contact: str = ""


@dataclass
class IdentityRegistry:
    """别名 ↔ 真实身份 的映射，与评审内容分开存放。"""

    _by_alias: dict[str, Identity] = field(default_factory=dict)

    def register(self, identity: Identity) -> None:
        self._by_alias[identity.alias] = identity

    def get(self, alias: str) -> Identity | None:
        return self._by_alias.get(alias)

    def resolve(self, alias: str, *, sensitivity: str, unblinded: bool, requester_role: str) -> Identity:
        """按案件敏感级别与解盲状态解析身份，越权即拒绝。"""
        policy = _IDENTITY_POLICY[sensitivity]
        allowed = policy["after"] if unblinded else policy["before"]
        if requester_role not in allowed:
            raise IdentitySealError(
                f"敏感级别 {sensitivity} 下角色 {requester_role} 无权解析身份"
            )
        identity = self.get(alias)
        if identity is None:
            raise IdentitySealError(f"别名未注册: {alias}")
        return identity

    def leak_scan(self, text: str) -> list[str]:
        """扫描文本中是否混入任何已注册的真实姓名或联系方式。"""
        hits: list[str] = []
        for identity in self._by_alias.values():
            for token in (identity.real_name, identity.contact):
                if token and token in text:
                    hits.append(token)
        return hits
