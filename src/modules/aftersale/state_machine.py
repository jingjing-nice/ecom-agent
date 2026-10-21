"""售后状态机（方案 §5）。

状态迁移由服务端检查当前状态及 version（乐观锁），
CREATED 表示外部售后单已创建，不代表退款完成或换货完成。
"""
from __future__ import annotations

from src.core.exceptions import InvalidState

# 状态常量
DRAFT = "DRAFT"
SUBMITTED = "SUBMITTED"
REVIEWING = "REVIEWING"
NEED_MORE_INFO = "NEED_MORE_INFO"
APPROVED = "APPROVED"
CREATING = "CREATING"
CREATED = "CREATED"
UNKNOWN = "UNKNOWN"
REJECTED = "REJECTED"
CANCELLED = "CANCELLED"

ALL_STATUSES = {
    DRAFT,
    SUBMITTED,
    REVIEWING,
    NEED_MORE_INFO,
    APPROVED,
    CREATING,
    CREATED,
    UNKNOWN,
    REJECTED,
    CANCELLED,
}

# 合法迁移表
TRANSITIONS: dict[str, set[str]] = {
    DRAFT: {SUBMITTED, CANCELLED},
    SUBMITTED: {REVIEWING, CANCELLED},
    REVIEWING: {NEED_MORE_INFO, APPROVED, REJECTED, CANCELLED},
    NEED_MORE_INFO: {REVIEWING, CANCELLED},
    APPROVED: {CREATING, REVIEWING},
    CREATING: {CREATED, UNKNOWN},
    # UNKNOWN 时先按请求号查询外部状态，再决定回 CREATED 或 REVIEWING
    UNKNOWN: {CREATED, REVIEWING, APPROVED},
    CREATED: set(),
    REJECTED: set(),
    CANCELLED: set(),
}

# 终态：不可再迁移
TERMINAL_STATUSES = {CREATED, REJECTED, CANCELLED}

# 买家可自行取消的状态
BUYER_CANCELLABLE = {DRAFT, SUBMITTED, REVIEWING, NEED_MORE_INFO}


def can_transition(from_status: str, to_status: str) -> bool:
    """判断迁移是否合法。"""
    return to_status in TRANSITIONS.get(from_status, set())


def ensure_transition(from_status: str, to_status: str) -> None:
    """校验迁移合法性，非法时抛 InvalidState。"""
    if from_status not in ALL_STATUSES:
        raise InvalidState(f"未知状态：{from_status}")
    if not can_transition(from_status, to_status):
        raise InvalidState(f"非法状态迁移：{from_status} → {to_status}")


def transition_case(case, to_status: str) -> None:
    """校验并推进状态、递增版本；调用方负责持有行锁与事务。"""
    ensure_transition(case.status, to_status)
    case.status, case.version = to_status, case.version + 1
