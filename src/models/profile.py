"""买家画像（模拟数据）：地区 / 年龄段 / 复购 / 客单价分层。

不采集真实敏感身份数据；分析侧返回分组统计，小于 10 人的群组隐藏。
"""
from __future__ import annotations

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base
from src.models.base import TimestampMixin, UUIDPKMixin


class BuyerProfile(Base, UUIDPKMixin, TimestampMixin):
    """模拟买家画像。"""

    __tablename__ = "buyer_profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "buyer_id", name="uq_profiles_tenant_buyer"),
    )

    tenant_id: Mapped[str] = mapped_column(index=True, nullable=False)
    buyer_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # 地区：华东 / 华南 / 华北 / 西南 / 华中
    region: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    # 年龄段：18-24 / 25-34 / 35-44 / 45+
    age_group: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    # 复购分层：new / returning / vip
    repeat_level: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    # 客单价分层：lt_100 / 100_300 / 300_1000 / gte_1000
    aov_band: Mapped[str] = mapped_column(String(16), index=True, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BuyerProfile {self.buyer_id} {self.region}/{self.age_group}>"