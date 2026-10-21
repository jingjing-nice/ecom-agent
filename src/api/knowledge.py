from datetime import datetime, timezone
from uuid import UUID
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.responses import response
from src.api.schemas import KnowledgeImport
from src.core.database import get_db
from src.core.deps import ActorScope, require_roles
from src.core.exceptions import Conflict, NotFound
from src.models import PolicyVersion, PolicyDocument, PolicyChunk, AuditLog, Job
from src.services.knowledge import import_document, publish_blockers

router = APIRouter()


@router.get("/knowledge/versions")
async def versions(request: Request, limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0),
                   actor: ActorScope = Depends(require_roles("knowledge_admin")), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(PolicyVersion, PolicyDocument.title, PolicyDocument.doc_id)
        .join(PolicyDocument, (PolicyDocument.id == PolicyVersion.policy_document_id)
              & (PolicyDocument.tenant_id == PolicyVersion.tenant_id))
        .where(PolicyVersion.tenant_id == actor.tenant_id)
        .order_by(PolicyVersion.created_at.desc(), PolicyVersion.id).limit(limit).offset(offset))).all()
    return response(request, {"items": [{"id": str(v.id), "title": title, "doc_id": doc_id,
        "version": v.version, "scope": v.scope, "status": v.status,
        "index_version": v.index_version, "effective_from": v.effective_from,
        "effective_to": v.effective_to} for v, title, doc_id in rows], "limit": limit, "offset": offset})


@router.get("/knowledge/{version_id}")
async def preview(version_id: UUID, request: Request,
                  actor: ActorScope = Depends(require_roles("knowledge_admin")), db: AsyncSession = Depends(get_db)):
    version = await db.scalar(select(PolicyVersion).where(PolicyVersion.id == version_id,
        PolicyVersion.tenant_id == actor.tenant_id))
    if version is None:
        raise NotFound("政策版本不存在")
    chunks = list((await db.scalars(select(PolicyChunk).where(PolicyChunk.policy_version_id == version_id,
        PolicyChunk.tenant_id == actor.tenant_id).order_by(PolicyChunk.chunk_index))).all())
    task = await db.scalar(select(Job).where(Job.tenant_id == actor.tenant_id,
        Job.payload["version_id"].astext == str(version.id)).order_by(Job.created_at.desc()).limit(1))
    return response(request, {"id": str(version.id), "version": version.version, "status": version.status,
        "automation_rules": version.automation_rules,
        "pipeline_status": task.status if task else None,
        "chunking": (task.payload or {}).get("chunking") if task else None,
        "index_version": version.index_version, "publish_blocker": publish_blockers(version, chunks),
        "chunks": [{"id": str(c.id), "title": c.section_title, "content": c.content,
                    "char_start": c.char_start, "char_end": c.char_end, "model_version": c.model_version}
                   for c in chunks]})


@router.post("/knowledge/documents", status_code=201)
async def import_knowledge(payload: KnowledgeImport, request: Request,
                           actor: ActorScope = Depends(require_roles("knowledge_admin")), db: AsyncSession = Depends(get_db)):
    """导入知识文本：创建 draft 版本，投递模型语义切片和索引任务（方案 §4）。"""
    result = await import_document(db, actor, payload, request.state.request_id)
    await db.commit()
    return response(request, result)


@router.post("/knowledge/{version_id}/review")
async def submit_review(version_id: UUID, request: Request,
                        actor: ActorScope = Depends(require_roles("knowledge_admin")), db: AsyncSession = Depends(get_db)):
    """提交审核：draft → reviewing；发布前仍会校验索引与有效期重叠。"""
    version = await db.scalar(select(PolicyVersion).where(PolicyVersion.id == version_id,
        PolicyVersion.tenant_id == actor.tenant_id).with_for_update())
    if not version:
        raise NotFound("政策版本不存在")
    if version.status != "draft":
        raise Conflict("仅草稿版本可以提交审核")
    chunk_count = await db.scalar(select(func.count()).select_from(PolicyChunk).where(
        PolicyChunk.policy_version_id == version.id, PolicyChunk.tenant_id == actor.tenant_id))
    if not chunk_count:
        raise Conflict("政策没有可审核片段")
    version.status = "reviewing"
    db.add(AuditLog(tenant_id=actor.tenant_id, actor_id=actor.user_id, action="knowledge.submitted_review",
                    resource_type="policy_version", resource_id=str(version.id),
                    created_at=datetime.now(timezone.utc), trace_id=request.state.request_id))
    await db.commit()
    return response(request, {"version_id": str(version.id), "status": version.status})


@router.post("/knowledge/{version_id}/publish")
async def publish(version_id: UUID, request: Request, actor: ActorScope = Depends(require_roles("knowledge_admin")), db: AsyncSession = Depends(get_db)):
    version = await db.scalar(select(PolicyVersion).where(PolicyVersion.id == version_id, PolicyVersion.tenant_id == actor.tenant_id).with_for_update())
    if not version:
        raise NotFound("政策版本不存在")
    # 文档锁防止两个版本同时通过发布检查。
    await db.scalar(select(PolicyDocument).where(PolicyDocument.id == version.policy_document_id,
        PolicyDocument.tenant_id == actor.tenant_id).with_for_update())
    chunks = (await db.scalars(select(PolicyChunk).where(PolicyChunk.policy_version_id == version.id,
        PolicyChunk.tenant_id == actor.tenant_id))).all()
    # 审核状态、索引完成与片段完整性由服务层统一判定（keyword-only 降级可发布）。
    blocker = publish_blockers(version, list(chunks))
    if blocker:
        raise Conflict(blocker)
    if version.effective_to and version.effective_to <= version.effective_from:
        raise Conflict("政策有效区间不合法")
    query = select(PolicyVersion).where(PolicyVersion.tenant_id == actor.tenant_id,
        PolicyVersion.policy_document_id == version.policy_document_id, PolicyVersion.id != version.id,
        PolicyVersion.status == "published", or_(PolicyVersion.effective_to.is_(None), PolicyVersion.effective_to > version.effective_from))
    if version.effective_to:
        query = query.where(PolicyVersion.effective_from < version.effective_to)
    if await db.scalar(query):
        raise Conflict("已发布政策存在有效期重叠，请先撤销或修订")
    version.status, version.published_by, version.published_at = "published", actor.user_id, datetime.now(timezone.utc)
    db.add(AuditLog(tenant_id=actor.tenant_id, actor_id=actor.user_id, action="knowledge.published",
                    resource_type="policy_version", resource_id=str(version.id), created_at=datetime.now(timezone.utc), trace_id=request.state.request_id))
    await db.commit()
    return response(request, {"version_id": str(version.id), "status": version.status})


@router.post("/knowledge/{version_id}/revoke")
async def revoke(version_id: UUID, request: Request, actor: ActorScope = Depends(require_roles("knowledge_admin")), db: AsyncSession = Depends(get_db)):
    version = await db.scalar(select(PolicyVersion).where(PolicyVersion.id == version_id,
        PolicyVersion.tenant_id == actor.tenant_id).with_for_update())
    if not version:
        raise NotFound("政策版本不存在")
    version.status = "revoked"
    db.add(AuditLog(tenant_id=actor.tenant_id, actor_id=actor.user_id, action="knowledge.revoked",
                    resource_type="policy_version", resource_id=str(version.id), created_at=datetime.now(timezone.utc), trace_id=request.state.request_id))
    await db.commit()
    return response(request, {"version_id": str(version.id), "status": version.status})
