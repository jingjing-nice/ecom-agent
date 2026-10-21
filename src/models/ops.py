"""运维支撑：任务队列 / 外发事件 outbox / 工具调用记录 / 审计日志。

任务可靠性（方案 §8）：
队列按至少一次投递设计；Worker 只消费 job_id，参数与状态存 PostgreSQL；
通过锁和租约领取任务，进程故障后可回收。
重试策略：最多 5 次指数退避 + 抖动；权限错误/无效参数/政策冲突不重试。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base
from src.models.base import TimestampMixin, UUIDPKMixin


class Job(Base, UUIDPKMixin, TimestampMixin):
    """异步任务。状态：pending / running / succeeded / failed。"""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "job_id", name="uq_jobs_tenant_job"),
        Index("ix_jobs_status_retry", "status", "next_retry_at"),
        Index("ix_jobs_type", "tenant_id", "job_type"),
    )

    tenant_id: Mapped[str] = mapped_column(index=True, nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), index=True, default="pending")
    payload: Mapped[dict | None] = mapped_column(JSONB, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 租约锁：locked_by + locked_at，进程故障后可回收
    locked_by: Mapped[str | None] = mapped_column(String(64))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Job {self.job_id} {self.job_type} {self.status}>"


class OutboxEvent(Base, UUIDPKMixin, TimestampMixin):
    """事务性外发事件：业务行与事件同事务写入，消费端按 event_id 去重、
    按 aggregate_version 拒绝旧事件覆盖新版本。"""

    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "event_id", name="uq_outbox_tenant_event"),
        Index("ix_outbox_status", "status", "created_at"),
    )

    tenant_id: Mapped[str] = mapped_column(index=True, nullable=False)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    aggregate_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<OutboxEvent {self.event_type} {self.event_id}>"


class ToolCallRecord(Base, UUIDPKMixin):
    """工具调用记录（前端工具轨迹 + 审计）。输入输出做脱敏摘要。"""

    __tablename__ = "tool_calls"
    __table_args__ = (Index("ix_tool_calls_trace", "trace_id"),)

    tenant_id: Mapped[str] = mapped_column(index=True, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    agent: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # 执行时的授权范围快照
    actor_scope: Mapped[dict | None] = mapped_column(JSONB)
    input_summary: Mapped[dict | None] = mapped_column(JSONB)
    result_summary: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="ok", nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ToolCall {self.agent}/{self.tool_name} {self.status}>"


class AuditLog(Base, UUIDPKMixin):
    """审计日志：审批、发布、接管、权限变更等关键动作留痕。"""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_actor", "tenant_id", "actor_id", "created_at"),
        Index("ix_audit_resource", "resource_type", "resource_id"),
    )

    tenant_id: Mapped[str] = mapped_column(index=True, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSONB)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AuditLog {self.action} {self.resource_type}/{self.resource_id}>"