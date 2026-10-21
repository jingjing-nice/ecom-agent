"""订单与退款：订单主表 / 订单明细（成交快照）/ 退款明细。

时间统一 UTC；金额整数最小货币单位。
外部订单号以 (tenant_id, platform, external_order_id) 唯一。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base
from src.models.base import TimestampMixin, UUIDPKMixin


class Order(Base, UUIDPKMixin, TimestampMixin):
    """订单主信息。"""

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_orders_tenant_id"),
        UniqueConstraint(
            "tenant_id", "platform", "order_no", name="uq_orders_tenant_platform_no"
        ),
        # 常用索引：订单用户与时间
        Index("ix_orders_buyer_paid", "tenant_id", "buyer_id", "paid_at"),
    )

    tenant_id: Mapped[str] = mapped_column(index=True, nullable=False)
    shop_id: Mapped[str] = mapped_column(index=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(32), default="sandbox", nullable=False)
    order_no: Mapped[str] = mapped_column(String(64), nullable=False)
    buyer_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="paid")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_amount_minor: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="CNY", nullable=False)
    # 物流与签收（售后核验用）
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Order {self.order_no} status={self.status}>"


class OrderItem(Base, UUIDPKMixin):
    """订单明细（保存成交快照）。"""

    __tablename__ = "order_items"
    __table_args__ = (
        Index("ix_order_items_order", "order_id"),
        UniqueConstraint("tenant_id", "id", name="uq_order_items_tenant_id"),
        ForeignKeyConstraint(["tenant_id", "order_id"], ["orders.tenant_id", "orders.id"]),
    )

    tenant_id: Mapped[str] = mapped_column(index=True, nullable=False)
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    sku_id: Mapped[str] = mapped_column(String(96), index=True, nullable=False)
    product_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    paid_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 成交快照（品类归属默认采用成交快照）
    title_snapshot: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    brand_snapshot: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    category_snapshot: Mapped[str] = mapped_column(
        String(64), index=True, default="", nullable=False
    )
    specs_snapshot: Mapped[dict | None] = mapped_column(JSONB, default=dict)

    order: Mapped[Order] = relationship(back_populates="items")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<OrderItem {self.sku_id} x{self.quantity}>"


class RefundItem(Base, UUIDPKMixin):
    """退款明细（按订单明细记录，支持部分退款）。"""

    __tablename__ = "refund_items"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "order_item_id"], ["order_items.tenant_id", "order_items.id"]),
        Index("ix_refund_items_item", "order_item_id"),
        Index("ix_refund_items_completed", "tenant_id", "completed_at"),
    )

    tenant_id: Mapped[str] = mapped_column(index=True, nullable=False)
    order_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    refund_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    refund_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="completed", nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(255), default="", nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<RefundItem {self.order_item_id} amount={self.refund_amount_minor}>"
