"""盲审流程的异常类型。"""


class BlindReviewError(Exception):
    """所有盲审领域错误的基类。"""


class CaseStateError(BlindReviewError):
    """当前案件阶段不允许该操作。"""


class CaseSealedError(CaseStateError):
    """案件已封存或已解盲，结果不可再变更。"""


class WindowError(BlindReviewError):
    """操作不在规定窗口内（未开放或已截止）。"""


class AccessDeniedError(BlindReviewError):
    """未完成冲突申报、已被回避或角色不符，禁止接触内容。"""


class AuthorizationError(BlindReviewError):
    """操作者角色或授权令牌不满足要求。"""


class QuorumNotMetError(BlindReviewError):
    """有效票数未达到法定人数，禁止解盲。"""


class IdentitySealError(BlindReviewError):
    """案件敏感级别禁止在当前阶段解析真实身份。"""


class IdentityLeakError(BlindReviewError):
    """通知载荷中出现真实身份信息，禁止发送。"""


class DuplicateVoteError(BlindReviewError):
    """同一 vote_id 携带不同载荷重复提交。"""
