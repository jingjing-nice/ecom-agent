"""政策知识：文档 / 版本 / 片段（含 pgvector 向量）。

发布流程（方案 §4）：
上传 → 解析 → 切分 → 人工预览 → Embedding → 新索引版本 → 抽样评测 → 切换发布指针。
同一主题版本冲突时停止给确定结论，进入运营修订。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.core.config import get_settings
from src.core.database import Base
from src.models.base import TimestampMixin, UUIDPKMixin

_EMBED_DIM = get_settings().EMBEDDING_DIM


class PolicyDocument(Base, UUIDPKMixin, TimestampMixin):
    """政策文档（作用域：售后 / 支付 / 物流 / 会员）。"""

    __tablename__ = "policy_documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "doc_id", name="uq_policy_docs_tenant_doc"),
    )

    tenant_id: Mapped[str] = mapped_column(index=True, nullable=False)
    doc_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), index=True, default="after_sale")
    source: Mapped[str] = mapped_column(String(255), default="自建政策样例", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PolicyDocument {self.doc_id} {self.title}>"


class PolicyVersion(Base, UUIDPKMixin, TimestampMixin):
    """政策版本。发布指针切换以本表 status/effective 区间为准。"""

    __tablename__ = "policy_versions"
    __table_args__ = (
        UniqueConstraint(
            "policy_document_id", "version", name="uq_policy_versions_doc_ver"
        ),
        Index("ix_policy_versions_tenant_scope_status", "tenant_id", "scope", "status"),
    )

    tenant_id: Mapped[str] = mapped_column(index=True, nullable=False)
    policy_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policy_documents.id"), nullable=False
    )
    # 冗余作用域，便于检索过滤
    scope: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    # draft / reviewing / published / revoked
    status: Mapped[str] = mapped_column(String(32), index=True, default="draft")
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checksum: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    # Embedding 模型版本（切换模型时建新索引集合）
    index_version: Mapped[str | None] = mapped_column(String(64))
    automation_rules: Mapped[dict | None] = mapped_column(JSONB)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_by: Mapped[str | None] = mapped_column(String(64))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PolicyVersion {self.version} status={self.status}>"


class PolicyChunk(Base, UUIDPKMixin, TimestampMixin):
    """政策片段（保留原文位置与内容哈希）。"""

    __tablename__ = "policy_chunks"
    __table_args__ = (
        Index("ix_policy_chunks_version", "policy_version_id"),
        Index("ix_policy_chunks_hash", "content_hash"),
    )

    tenant_id: Mapped[str] = mapped_column(index=True, nullable=False)
    policy_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policy_versions.id"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    section_title: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 原文定位
    char_start: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    # pgvector 向量（HNSW 索引在迁移脚本中创建）
    embedding: Mapped[list | None] = mapped_column(Vector(_EMBED_DIM))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PolicyChunk v{self.chunk_index} {self.section_title[:20]}>"