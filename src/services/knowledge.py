"""知识入库登记：原文与任务同事务保存，Worker 运行 LangGraph 模型切片及索引。

在线入库不再调用规则切片。split_text 仅作为离线 LangChain 分窗基线兼容入口。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.deps import ActorScope
from src.core.exceptions import AppError, Conflict
from src.models import AuditLog, PolicyChunk, PolicyDocument, PolicyVersion

from src.services.jobs import enqueue_job

KEYWORD_ONLY = "keyword-only"


def split_text(content: str, *, max_chars: int | None = None) -> list[dict]:
    """兼容离线基线：委托 LangChain splitter；在线入库使用 Worker 模型语义切片。"""
    from src.retrieval.chunking import chunk_records, split_documents
    limit = get_settings().CHUNK_MAX_CHARS if max_chars is None else max_chars
    return chunk_records(split_documents(content, max_chars=limit))


def parse_effective_range(effective_from, effective_to):
    """规范化有效期：默认从当前时间开始；结束早于开始直接拒绝。"""
    start = effective_from or datetime.now(timezone.utc)
    start = start.replace(tzinfo=timezone.utc) if start.tzinfo is None else start.astimezone(timezone.utc)
    if effective_to:
        effective_to = effective_to.replace(tzinfo=timezone.utc) if effective_to.tzinfo is None else effective_to.astimezone(timezone.utc)
    if effective_to and effective_to <= start:
        raise AppError("政策有效区间不合法：结束时间必须晚于开始时间")
    return start, effective_to


def publish_blockers(version: PolicyVersion, chunks: list[PolicyChunk]) -> str | None:
    """发布前置检查：返回拒绝原因；None 表示可发布。

    片段要么完成向量索引，要么在未配置 Embedding 时显式降级为 keyword-only。
    """
    if version.status != "reviewing":
        return "政策需先提交审核（reviewing）后才能发布"
    if not version.index_version:
        return "政策需完成切片和索引构建后才能发布"
    if not chunks:
        return "政策没有可发布片段"
    pending = [c for c in chunks if c.embedding is None and c.model_version != KEYWORD_ONLY]
    if pending:
        return "仍有片段未完成索引构建"
    return None


async def import_document(db: AsyncSession, actor: ActorScope, payload, trace_id: str) -> dict:
    """登记知识文档与 draft 版本，切分片段并投递异步索引任务。"""
    if not actor.is_knowledge_admin:
        raise AppError("仅知识管理员可以导入知识文本")
    if payload.automation_rules and payload.scope != "after_sale":
        raise AppError("自动售后规则只能绑定售后政策")
    content = payload.content
    if not content.strip():
        raise AppError("知识文本不能为空")
    document = await db.scalar(select(PolicyDocument).where(
        PolicyDocument.tenant_id == actor.tenant_id, PolicyDocument.doc_id == payload.doc_id))
    if document is None:
        document = PolicyDocument(tenant_id=actor.tenant_id, doc_id=payload.doc_id,
                                  title=payload.title, scope=payload.scope,
                                  source=payload.source, description=payload.description)
        db.add(document)
        await db.flush()
    if document.scope != payload.scope:
        raise Conflict("同一文档的适用范围不能改变，请使用新的文档标识")
    conflicting = await db.scalar(select(PolicyVersion.id).where(
        PolicyVersion.policy_document_id == document.id, PolicyVersion.version == payload.version))
    if conflicting:
        raise Conflict("该文档已存在同一版本号，请修订版本后再导入")
    effective_from, effective_to = parse_effective_range(payload.effective_from, payload.effective_to)
    version = PolicyVersion(tenant_id=actor.tenant_id, policy_document_id=document.id, scope=payload.scope,
                            version=payload.version, status="draft", effective_from=effective_from,
                            effective_to=effective_to, automation_rules=payload.automation_rules.model_dump() if payload.automation_rules else None, checksum=hashlib.sha256(content.encode()).hexdigest())
    db.add(version)
    await db.flush()
    job = enqueue_job(db, tenant_id=actor.tenant_id, job_type="knowledge_index",
               payload={"version_id": str(version.id), "owner_id": actor.user_id,
                        "content": content, "content_hash": version.checksum})
    db.add(AuditLog(tenant_id=actor.tenant_id, actor_id=actor.user_id, action="knowledge.imported",
                    resource_type="policy_version", resource_id=str(version.id),
                    detail={"doc_id": document.doc_id, "version": version.version, "chunking": "pending_model"},
                    created_at=datetime.now(timezone.utc), trace_id=trace_id))
    return {"document_id": str(document.id), "version_id": str(version.id), "version": version.version,
            "status": version.status, "chunk_count": 0, "chunking_status": "pending", "job_id": job.job_id,
            "effective_from": effective_from.isoformat()}