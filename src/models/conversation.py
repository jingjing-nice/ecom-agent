"""会话与消息：持久化事实存于 PostgreSQL（不只存在 Redis）。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base
from src.models.base import TimestampMixin, UUIDPKMixin


class Conversation(Base, UUIDPKMixin, TimestampMixin):
    """会话。takeover_status=human 时停止自动业务答复。"""

    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_user", "tenant_id", "user_id", "updated_at"),
    )

    tenant_id: Mapped[str] = mapped_column(index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    channel: Mapped[str] = mapped_column(String(32), default="web", nullable=False)
    title: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    # auto / human（人工接管后停止自动业务答复）
    takeover_status: Mapped[str] = mapped_column(String(16), default="auto", nullable=False)
    takeover_by: Mapped[str | None] = mapped_column(String(64))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Conversation {self.id} user={self.user_id}>"


class Message(Base, UUIDPKMixin, TimestampMixin):
    """消息。seq 为会话内序号（幂等去重用 client_message_id）。"""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_messages_conv_seq"),
        UniqueConstraint("conversation_id", "client_message_id", name="uq_messages_client_id"),
        Index("ix_messages_conv", "conversation_id", "seq"),
    )

    tenant_id: Mapped[str] = mapped_column(index=True, nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 结构化附加信息：商品卡片、政策依据、工具轨迹摘要
    meta: Mapped[dict | None] = mapped_column(JSONB)
    # 客户端消息 ID（幂等去重）
    client_message_id: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Message conv={self.conversation_id} seq={self.seq} role={self.role}>"
