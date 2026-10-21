from datetime import datetime, timezone
from uuid import UUID
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.schemas import ManualField, Presign, VerifyEvidence
from src.api.responses import response
from src.core.database import get_db
from src.core.deps import require_roles, ActorScope
from src.core.exceptions import AppError, NotFound, Conflict
from src.infra.storage import ALLOWED_EXTENSIONS, MAX_FILE_SIZE_BYTES, build_object_key, presign_put, stat_object
from src.models import AuditLog, EvidenceObject, Job
from src.services.orders import owned_item
from src.services.jobs import enqueue_job
from src.services.vision import apply_manual_field, stamp_verification, verify_evidence

router = APIRouter()


@router.post("/uploads/presign")
async def presign(payload: Presign, request: Request, actor: ActorScope = Depends(require_roles("buyer")), db: AsyncSession = Depends(get_db)):
    if payload.file_name.rsplit(".", 1)[-1].lower() not in ALLOWED_EXTENSIONS:
        raise AppError("仅支持 jpg、png 和 webp 图片")
    key = build_object_key(actor.tenant_id, actor.user_id, payload.file_name)
    url = await presign_put(key)
    record = EvidenceObject(tenant_id=actor.tenant_id, owner_id=actor.user_id, kind=payload.kind,
                            file_name=payload.file_name, object_key=key, status="pending_upload")
    db.add(record)
    await db.commit()
    return response(request, {"image_id": str(record.id), "upload_url": url, "max_bytes": MAX_FILE_SIZE_BYTES, "expires_seconds": 600})


@router.post("/uploads/{image_id}/complete")
async def complete(image_id: UUID, request: Request, actor: ActorScope = Depends(require_roles("buyer")), db: AsyncSession = Depends(get_db)):
    record = await db.scalar(select(EvidenceObject).where(EvidenceObject.id == image_id,
        EvidenceObject.tenant_id == actor.tenant_id, EvidenceObject.owner_id == actor.user_id).with_for_update())
    if record is None:
        raise NotFound("图片不存在")
    if record.status != "pending_upload":
        raise Conflict("上传已完成或已进入处理流程")
    info = await stat_object(record.object_key)
    if info.size > MAX_FILE_SIZE_BYTES:
        raise AppError("图片超过大小限制")
    record.status = "extracting"
    job = enqueue_job(db, tenant_id=actor.tenant_id, job_type="vision_extract",
        payload={"image_id": str(image_id), "owner_id": actor.user_id})
    await db.commit()
    return response(request, {"job_id": job.job_id, "status": job.status})


@router.get("/uploads/{image_id}")
async def image(image_id: UUID, request: Request, actor: ActorScope = Depends(require_roles("buyer")), db: AsyncSession = Depends(get_db)):
    record = await db.scalar(select(EvidenceObject).where(EvidenceObject.id == image_id,
        EvidenceObject.tenant_id == actor.tenant_id, EvidenceObject.owner_id == actor.user_id))
    if record is None:
        raise NotFound("图片不存在")
    task = await db.scalar(select(Job).where(Job.tenant_id == actor.tenant_id,
        Job.payload["image_id"].astext == str(record.id), Job.payload["owner_id"].astext == actor.user_id)
        .order_by(Job.created_at.desc()).limit(1))
    return response(request, {"id": str(record.id), "status": record.status, "kind": record.kind,
        "extracted": record.extracted, "job_id": task.job_id if task else None,
        "error": task.error if task else None})


async def own_evidence(db, actor, image_id):
    record = await db.scalar(select(EvidenceObject).where(EvidenceObject.id == image_id,
        EvidenceObject.tenant_id == actor.tenant_id, EvidenceObject.owner_id == actor.user_id).with_for_update())
    if record is None:
        raise NotFound("图片不存在")
    if record.status != "extracted" or not record.extracted:
        raise Conflict("图片尚未完成识别，无法核对或补录")
    return record


@router.post("/uploads/{image_id}/verify")
async def verify(image_id: UUID, payload: VerifyEvidence, request: Request,
                 actor: ActorScope = Depends(require_roles("buyer")), db: AsyncSession = Depends(get_db)):
    """OCR 金额/订单号与订单事实精确核对；不判断凭证真伪与责任（方案 §5）。"""
    record = await own_evidence(db, actor, image_id)
    order_no = amount_minor = None
    if payload.order_item_id:
        item, order = await owned_item(db, actor, payload.order_item_id)
        order_no, amount_minor = order.order_no, item.paid_amount_minor
    result = verify_evidence(record.extracted, order_no=order_no, amount_minor=amount_minor)
    record.extracted = stamp_verification(record.extracted, result, order_item_id=payload.order_item_id)
    db.add(AuditLog(tenant_id=actor.tenant_id, actor_id=actor.user_id, action="evidence.verified",
                    resource_type="evidence_object", resource_id=str(record.id),
                    detail={"status": result["status"], "order_item_id": str(payload.order_item_id) if payload.order_item_id else None},
                    created_at=datetime.now(timezone.utc), trace_id=request.state.request_id))
    await db.commit()
    return response(request, {"image_id": str(record.id), "verification": record.extracted["verification"]})


@router.post("/uploads/{image_id}/fields")
async def manual_field(image_id: UUID, payload: ManualField, request: Request,
                       actor: ActorScope = Depends(require_roles("buyer")), db: AsyncSession = Depends(get_db)):
    """人工补录 OCR 字段：保留 OCR 原值，补录值标注来源（方案 §5）。"""
    record = await own_evidence(db, actor, image_id)
    updated = apply_manual_field(record.extracted, field_name=payload.field_name, value=payload.value, by=actor.user_id)
    record.extracted = updated
    db.add(AuditLog(tenant_id=actor.tenant_id, actor_id=actor.user_id, action="evidence.manual_field",
                    resource_type="evidence_object", resource_id=str(record.id),
                    detail={"field_name": payload.field_name, "note": payload.note},
                    created_at=datetime.now(timezone.utc), trace_id=request.state.request_id))
    await db.commit()
    field = next(item for item in updated["fields"] if item["field_name"] == payload.field_name)
    return response(request, {"image_id": str(record.id), "field": field,
                              "verification": updated.get("verification")})
