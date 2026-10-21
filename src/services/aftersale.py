"""售后事务服务：归属校验、并发版本、幂等提交、审核审计和 outbox。"""
import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select

from src.core.exceptions import AppError, Conflict, NotFound, PermissionDenied
from src.models import AfterSaleCase, AuditLog, OutboxEvent
from src.modules.aftersale.state_machine import BUYER_CANCELLABLE, ensure_transition, transition_case
from src.services.orders import owned_item
from src.services.jobs import enqueue_job
from src.services.evidence import load_refund_evidence
from src.services.vision import stamp_verification, verify_evidence


def case_data(case):
    return {"id": str(case.id), "case_no": case.case_no, "status": case.status,
            "version": case.version, "case_type": case.case_type, "reason": case.reason,
            "order_item_id": str(case.order_item_id), "policy_version": case.policy_version,
            "draft": case.draft, "external_case_id": case.external_case_id,
            "review_comment": case.review_comment, "evidence_ids": case.evidence_ids or [],
            "appeal_path": (case.draft or {}).get("appeal_path"), "last_error": case.last_error}


def audit(db, actor, action, case, trace_id):
    db.add(AuditLog(tenant_id=actor.tenant_id, actor_id=actor.user_id, action=action,
                    resource_type="after_sale_case", resource_id=str(case.id),
                    detail={"status": case.status, "version": case.version},
                    trace_id=trace_id, created_at=datetime.now(timezone.utc)))


async def get_case(db, actor, case_id, *, lock=False):
    query = select(AfterSaleCase).where(AfterSaleCase.id == case_id, AfterSaleCase.tenant_id == actor.tenant_id)
    if actor.is_buyer:
        query = query.where(AfterSaleCase.buyer_id == actor.user_id)
    elif actor.is_staff:
        query = query.where(AfterSaleCase.shop_id.in_(actor.shop_ids))
    else:
        raise PermissionDenied("该角色没有售后权限")
    if lock:
        query = query.with_for_update()
    case = await db.scalar(query.execution_options(populate_existing=True))
    if case is None:
        raise NotFound("售后单不存在")
    return case


async def get_case_for_update(db, actor, case_id, expected_version):
    case = await get_case(db, actor, case_id, lock=True)
    if case.version != expected_version:
        raise Conflict("售后单已更新，请刷新后重试")
    return case


async def validate_evidence(db, actor, evidence_ids, item, order):
    ids = set(evidence_ids)
    records = await load_refund_evidence(db, actor, ids, lock=True)
    if len(records) != len(ids):
        raise NotFound("凭证不存在、类型不符或尚未完成识别")
    for evidence in records:
        if evidence.extracted and evidence.extracted.get("fields"):
            verification = verify_evidence(evidence.extracted, order_no=order.order_no,
                                           amount_minor=item.paid_amount_minor)
            evidence.extracted = stamp_verification(evidence.extracted, verification, order_item_id=item.id)


async def cancel_case(db, actor, case_id, payload, trace_id):
    if not actor.is_buyer:
        raise PermissionDenied("仅买家可以取消申请")
    case = await get_case_for_update(db, actor, case_id, payload.expected_version)
    if case.status not in BUYER_CANCELLABLE:
        raise Conflict("当前状态不能取消，请查询执行状态")
    if not payload.reason.strip():
        raise AppError("请填写取消原因")
    transition_case(case, "CANCELLED")
    case.draft = {**(case.draft or {}), "cancel_reason": payload.reason.strip()}
    audit(db, actor, "case.cancelled", case, trace_id)
    return case


async def supplement_case(db, actor, case_id, payload, trace_id):
    if not actor.is_buyer:
        raise PermissionDenied("仅买家可以补充材料")
    case = await get_case_for_update(db, actor, case_id, payload.expected_version)
    if case.status != "NEED_MORE_INFO":
        raise Conflict("仅待补料的申请可以补充材料")
    if not payload.comment.strip():
        raise AppError("请填写补充说明")
    combined = list(dict.fromkeys([*(case.evidence_ids or []), *(str(e) for e in payload.evidence_ids)]))
    if len(combined) > 10:
        raise AppError("每张申请最多关联 10 张凭证")
    item, order = await owned_item(db, actor, case.order_item_id)
    await validate_evidence(db, actor, [UUID(e) for e in combined], item, order)
    history = list((case.draft or {}).get("supplements", []))
    history.append({"comment": payload.comment.strip(), "evidence_ids": [str(e) for e in payload.evidence_ids],
                    "at": datetime.now(timezone.utc).isoformat()})
    case.draft = {**(case.draft or {}), "supplements": history}
    case.evidence_ids = combined
    transition_case(case, "REVIEWING")
    audit(db, actor, "case.supplemented", case, trace_id)
    from src.core.config import get_settings
    if get_settings().AFTERSALE_AUTO_REVIEW:
        from src.services.automatic_aftersale import enqueue_review
        enqueue_review(db, case)
    return case


async def create_draft(db, actor, payload, trace_id):
    if not actor.is_buyer:
        raise PermissionDenied("仅买家可以创建申请草稿")
    item, order = await owned_item(db, actor, payload.order_item_id)
    if order.status not in {"paid", "shipped", "delivered", "completed"} or not order.paid_at:
        raise AppError("订单尚未支付，不能申请售后")
    await validate_evidence(db, actor, payload.evidence_ids, item, order)
    case = AfterSaleCase(tenant_id=actor.tenant_id, shop_id=order.shop_id,
                        case_no="AS-" + uuid4().hex, order_id=order.id, order_item_id=item.id,
                        buyer_id=actor.user_id, actor_id=actor.user_id,
                        case_type=payload.case_type, reason=payload.reason, status="DRAFT", version=1,
                        idempotency_key="draft-" + uuid4().hex,
                        evidence_ids=[str(e) for e in payload.evidence_ids],
                        draft={"message": "申请草稿，确认后由系统按已发布规则自动核验", "requires_review": False})
    db.add(case)
    await db.flush()
    audit(db, actor, "draft.created", case, trace_id)
    return case


async def submit_case(db, actor, case_id, payload, key, trace_id):
    if not actor.is_buyer:
        raise PermissionDenied("仅买家可以确认提交")
    if not payload.confirmed:
        raise AppError("需要明确确认提交")
    if not key or len(key) > 128:
        raise AppError("需要 1 至 128 字符的 Idempotency-Key")
    # 同一用户的幂等键串行化，涵盖跨工单重用与并发请求。
    from sqlalchemy import text
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                     {"key": f"{actor.tenant_id}:{actor.user_id}:submit:{key}"})
    request_hash = hashlib.sha256(json.dumps({"case_id": str(case_id), **payload.model_dump()}, sort_keys=True).encode()).hexdigest()
    existing = await db.scalar(select(AfterSaleCase).where(
        AfterSaleCase.tenant_id == actor.tenant_id, AfterSaleCase.actor_id == actor.user_id,
        AfterSaleCase.operation == "submit_case", AfterSaleCase.idempotency_key == key))
    if existing:
        if existing.request_hash != request_hash:
            raise Conflict("同一幂等键不能用于不同请求")
        return existing
    case = await get_case_for_update(db, actor, case_id, payload.expected_version)
    transition_case(case, "SUBMITTED")
    case.idempotency_key, case.request_hash = key, request_hash
    audit(db, actor, "case.submitted", case, trace_id)
    from src.core.config import get_settings
    if get_settings().AFTERSALE_AUTO_REVIEW:
        from src.services.automatic_aftersale import enqueue_review
        enqueue_review(db, case)
    return case


async def review_case(db, actor, case_id, payload, trace_id):
    if not actor.is_staff:
        raise PermissionDenied("仅授权客服可以审核")
    case = await get_case_for_update(db, actor, case_id, payload.expected_version)
    ensure_transition(case.status, payload.decision)
    if payload.decision in {"APPROVED", "REJECTED", "NEED_MORE_INFO"} and not payload.comment.strip():
        raise AppError("审核结论需要填写依据；拒绝时需说明申诉路径")
    if payload.decision == "APPROVED" and not payload.policy_version:
        raise AppError("审批通过需要记录已核验政策版本")
    if payload.decision == "APPROVED":
        from src.models import PolicyVersion
        from sqlalchemy import or_
        now = datetime.now(timezone.utc)
        policies = (await db.scalars(select(PolicyVersion).where(
            PolicyVersion.tenant_id == actor.tenant_id, PolicyVersion.scope == "after_sale",
            PolicyVersion.version == payload.policy_version, PolicyVersion.status == "published",
            PolicyVersion.effective_from <= now,
            or_(PolicyVersion.effective_to.is_(None), PolicyVersion.effective_to > now)))).all()
        if len(policies) != 1:
            raise AppError("政策版本不存在、已过期或存在歧义，不能审批通过")
    if payload.decision == "REJECTED":
        if not payload.appeal_path.strip():
            raise AppError("拒绝申请必须提供申诉路径")
        case.draft = {**(case.draft or {}), "appeal_path": payload.appeal_path.strip()}
    transition_case(case, payload.decision)
    case.reviewer_id, case.review_comment = actor.user_id, payload.comment
    case.reviewed_at = datetime.now(timezone.utc)
    if payload.policy_version:
        case.policy_version = payload.policy_version
    if payload.decision == "APPROVED":
        enqueue_creation(db, case)
    audit(db, actor, "case.reviewed", case, trace_id)
    return case


def enqueue_creation(db, case):
    event_id = uuid4().hex
    db.add(OutboxEvent(tenant_id=case.tenant_id, event_id=event_id,
        aggregate_type="after_sale_case", aggregate_id=str(case.id), aggregate_version=case.version,
        event_type="case.approved", payload={"case_id": str(case.id)}, status="pending"))
    return enqueue_job(db, tenant_id=case.tenant_id, job_id=event_id, job_type="create_sandbox_case",
        payload={"case_id": str(case.id), "owner_id": case.buyer_id})
