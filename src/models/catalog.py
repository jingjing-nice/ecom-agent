"""商品目录：SPU / SKU / 库存快照。

价格一律使用最小货币单位整数并保存币种（方案 §3）。
跨实体关联使用包含 tenant_id 的复合外键，阻止跨租户引用。
"""
from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from src.core.config import get_settings

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base
from src.models.base import TimestampMixin, UUIDPKMixin


class Product(Base, UUIDPKMixin, TimestampMixin):
    """商品 SPU。"""

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("tenant_id", "product_id", name="uq_products_tenant_pid"),
    )

    tenant_id: Mapped[str] = mapped_column(index=True, nullable=False)
    shop_id: Mapped[str] = mapped_column(index=True, nullable=False)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    brand: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    category: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    sub_category: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 结构化属性：颜色/材质/适用场景等，用于图谱属性筛选与文案生成
    attributes: Mapped[dict | None] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(32), index=True, default="on_sale")
    image_key: Mapped[str | None] = mapped_column(String(255))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Product {self.product_id} {self.title}>"


class ProductSKU(Base, UUIDPKMixin, TimestampMixin):
    """商品 SKU。price_minor 为最小货币单位整数。"""

    __tablename__ = "product_skus"
    __table_args__ = (
        UniqueConstraint("tenant_id", "sku_id", name="uq_skus_tenant_sid"),
        # 复合外键：保证 SKU 与 SPU 同租户
        ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["products.tenant_id", "products.product_id"],
            name="fk_skus_tenant_product",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(index=True, nullable=False)
    shop_id: Mapped[str] = mapped_column(index=True, nullable=False)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sku_id: Mapped[str] = mapped_column(String(96), nullable=False)
    specs: Mapped[dict | None] = mapped_column(JSONB, default=dict)
    price_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="CNY", nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="on_sale")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SKU {self.sku_id} price={self.price_minor}>"


class InventorySnapshot(Base, UUIDPKMixin):
    """库存最新快照（源系统版本号 + 观测时间）。

    关键动作（下单/换货）前必须重新查询源系统，本题中即重读本表并校验 observed_at。
    """

    __tablename__ = "inventory_snapshots"
    __table_args__ = (
        UniqueConstraint("tenant_id", "sku_id", name="uq_inventory_tenant_sku"),
    )

    tenant_id: Mapped[str] = mapped_column(index=True, nullable=False)
    shop_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    sku_id: Mapped[str] = mapped_column(String(96), index=True, nullable=False)
    available_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Inventory {self.sku_id} qty={self.available_qty}>"


class Review(Base, UUIDPKMixin, TimestampMixin):
    """商品评价（模拟数据）。

    评论仅作为用户反馈，不能替代产品参数或形成无依据的功效承诺（方案 §4）。
    """

    __tablename__ = "reviews"
    __table_args__ = (Index("ix_reviews_product", "tenant_id", "product_id"),)

    tenant_id: Mapped[str] = mapped_column(index=True, nullable=False)
    shop_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 买家匿名引用（脱敏）
    buyer_ref: Mapped[str] = mapped_column(String(32), default="anonymous", nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Review {self.product_id} rating={self.rating}>"

class ProductSearchIndex(Base, UUIDPKMixin, TimestampMixin):
    """可重建的商品语义索引；事实仍以 Product/SKU/库存为准。"""
    __tablename__ = "product_search_indexes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "product_id", name="uq_product_search_tenant_pid"),
        ForeignKeyConstraint(["tenant_id", "product_id"], ["products.tenant_id", "products.product_id"]),
    )
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list | None] = mapped_column(Vector(get_settings().EMBEDDING_DIM))
