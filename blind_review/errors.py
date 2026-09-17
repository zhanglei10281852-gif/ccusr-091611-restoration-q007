"""盲审引擎的异常类型。"""


class BlindReviewError(Exception):
    """所有盲审领域错误的基类。"""


class AccessDenied(BlindReviewError):
    """未完成冲突申报或已被回避，不能接触方案内容。"""


class AuthorizationError(BlindReviewError):
    """缺少所需的角色授权（如秘书授权）。"""


class WindowClosed(BlindReviewError):
    """操作超出规定窗口且不允许以旁注形式保留。"""


class InvalidState(BlindReviewError):
    """案件状态不允许该操作。"""


class CaseSealedError(BlindReviewError):
    """案件已封存，任何修改（投票、撤回、改稿）都被拒绝。"""


class QuorumNotMet(BlindReviewError):
    """有效票数未达法定人数。"""


class IdentityLeakError(BlindReviewError):
    """通知事件包含真实身份字段或身份字符串。"""


class StoreCorrupted(BlindReviewError):
    """事件日志哈希链校验失败。"""
