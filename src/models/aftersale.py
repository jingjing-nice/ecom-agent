"""售后：售后单（状态机 + 乐观锁 + 幂等）与证据对象。

状态机（方案 §5）：
DRAFT → SUBMITTED → REVIEWING
    ├→ NEED_MORE_INFO → REVIEWING
    ├→ APPROVED → CREATING → CREATED
    └→ REJECTED
CREATING → UNKNOWN → 查询外部状态 → CREATED / REVIEWING
任意可取消状态 → CANCELLED
CREATED 表示外部售后单已创建，不代表退款完成。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base
from src.models.base import TimestampMixin, UUIDPKMixin


class AfterSaleCase(Base, UUIDPKMixin, TimestampMixin):
    """售后单。

    - version 乐观锁：状态迁移时校验并 +1，阻止并发审核覆盖
    - 幂等键唯一约束：(tenant_id, actor_id, operation, idempotency_key)
      同键不同请求体（request_hash 不同）返回冲突
    """

    __tablename__ = "after_sale_cases"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "order_id"], ["orders.tenant_id", "orders.id"]),
        ForeignKeyConstraint(["tenant_id", "order_item_id"], ["order_items.tenant_id", "order_items.id"]),
        UniqueConstraint("tenant_id", "case_no", name="uq_cases_tenant_no"),
        UniqueConstraint(
            "tenant_id",
            "actor_id",
            "operation",
            "idempotency_key",
            name="uq_cases_idempotency",
        ),
        # 常用索引：工单状态与更新时间
        Index("ix_cases_status_updated", "tenant_id", "status", "updated_at"),
        Index("ix_cases_shop_status", "shop_id", "status"),
    )

    tenant_id: Mapped[str] = mapped_column(index=True, nullable=False)
    shop_id: Mapped[str] = mapped_column(index=True, nullable=False)
    case_no: Mapped[str] = mapped_column(String(64), nullable=False)
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    order_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    buyer_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    case_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), index=True, default="DRAFT", nullable=False
    )
    policy_version: Mapped[str | None] = mapped_column(String(64))
    # 乐观锁版本号
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # 外部平台售后单号（接口成功后才写入）
    external_case_id: Mapped[str | None] = mapped_column(String(64))
    # 幂等：actor + operation + key 唯一；request_hash 检测同键不同体
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), default="submit_case", nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    # 草稿内容：政策依据、建议动作、缺失材料、寄回信息
    draft: Mapped[dict | None] = mapped_column(JSONB, default=dict)
    evidence_ids: Mapped[list | None] = mapped_column(JSONB, default=list)
    reviewer_id: Mapped[str | None] = mapped_column(String(64))
    review_comment: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 外部调用失败原因（UNKNOWN 状态排查用）
    last_error: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AfterSaleCase {self.case_no} status={self.status}>"


class EvidenceObject(Base, UUIDPKMixin, TimestampMixin):
    """上传证据对象（售后凭证 / 商品图片）。

    上传流程：presign → 直传 → 服务端校验（MIME/大小/像素/解码）→ OCR。
    extracted 保存结构化字段结果：field_name/value/bbox/status/source_image_id/model_version。
    """

    __tablename__ = "evidence_objects"
    __table_args__ = (Index("ix_evidence_owner", "tenant_id", "owner_id"),)

    tenant_id: Mapped[str] = mapped_column(index=True, nullable=False)
    owner_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="refund_evidence", nullable=False)
    object_key: Mapped[str] = mapped_column(String(255), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # uploaded → extracting → extracted / failed
    status: Mapped[str] = mapped_column(String(32), default="uploaded", nullable=False)
    extracted: Mapped[dict | None] = mapped_column(JSONB)
    # 关联的售后单（可选）
    case_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Evidence {self.object_key} status={self.status}>"
