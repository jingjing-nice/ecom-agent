"""ORM 公共基础：UUID 主键与时间戳 Mixin。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


class UUIDPKMixin:
    """UUID 主键。"""

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class TimestampMixin:
    """创建/更新时间（UTC，带时区）。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# 命名为 TenantMixin 便于统一审计：所有业务表均携带租户标识
class TenantMixin:
    """租户隔离标识。"""

    tenant_id: Mapped[str] = mapped_column(index=True, nullable=False)