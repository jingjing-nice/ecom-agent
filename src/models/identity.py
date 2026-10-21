"""身份与店铺：用户（买家/客服/运营/知识管理员）与模拟店铺。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base
from src.models.base import TimestampMixin, UUIDPKMixin


class Shop(Base, UUIDPKMixin, TimestampMixin):
    """模拟店铺（演示环境，两个店铺用于验证权限隔离）。"""

    __tablename__ = "shops"
    __table_args__ = (UniqueConstraint("tenant_id", "shop_id", name="uq_shops_tenant_shop"),)

    tenant_id: Mapped[str] = mapped_column(index=True, nullable=False)
    shop_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), default="sandbox", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Shop {self.tenant_id}/{self.shop_id} {self.name}>"


class User(Base, UUIDPKMixin, TimestampMixin):
    """演示账号。

    role:
      - buyer           买家
      - staff           客服（scope 内店铺的工单）
      - ops             运营（授权店铺只读商品/订单与聚合数据）
      - knowledge_admin 知识管理员（发布政策，无订单权限）
    shop_ids: 数据范围（客服可管理的店铺列表；运营可分析的店铺列表）。
    """

    __tablename__ = "users"

    tenant_id: Mapped[str] = mapped_column(index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    shop_ids: Mapped[list | None] = mapped_column(JSONB, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.email} role={self.role}>"