import asyncio
from contextlib import aclosing
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.responses import response
from src.services.chat import own_conversation
from src.api.schemas import Login, Draft, Submit, Review, AnalyticsQuery, Chat, ChatApproval, Takeover, CancelCase, SupplementCase, EscalateConversation, StaffReply
from src.core.database import get_db
from src.core.deps import get_current_actor, require_roles, ActorScope
from src.core.exceptions import AppError, AuthError, Conflict, NotFound, PermissionDenied
from src.core.security import create_access_token, verify_password
from src.models import User, Order, Job, Conversation, Message, AuditLog
from src.services.orders import order_scope, order_data, owned_order
from src.services.aftersale import create_draft, submit_case, review_case, get_case, case_data, cancel_case, supplement_case

router = APIRouter()


@router.post("/auth/login")
async def login(payload: Login, request: Request, db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(User).where(User.email == payload.email))
    if user is None or not user.is_active or not await asyncio.to_thread(verify_password, payload.password, user.password_hash):
        raise AuthError("账号或密码错误")
    user.last_login_at = datetime.now(timezone.utc)
    token = create_access_token(user_id=str(user.id), tenant_id=user.tenant_id, role=user.role, shop_ids=user.shop_ids)
    await db.commit()
    return response(request, {"access_token": token, "token_type": "bearer", "role": user.role})


@router.get("/auth/me")
async def me(request: Request, actor: ActorScope = Depends(get_current_actor)):
    return response(request, actor.to_dict())


@router.get("/orders")
async def orders(request: Request, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                 actor: ActorScope = Depends(get_current_actor), db: AsyncSession = Depends(get_db)):
    records = (await db.scalars(select(Order).where(*order_scope(actor)).order_by(Order.created_at.desc(), Order.id).limit(limit).offset(offset))).all()
    return response(request, {"items": [order_data(o) for o in records], "limit": limit, "offset": offset})


@router.get("/orders/{order_id}")
async def order(order_id: UUID, request: Request, actor: ActorScope = Depends(get_current_actor), db: AsyncSession = Depends(get_db)):
    return response(request, order_data(await owned_order(db, actor, order_id)))


@router.get("/products/{product_id}")
async def product_detail(product_id: str, request: Request,
                         actor: ActorScope = Depends(require_roles("buyer", "ops")), db: AsyncSession = Depends(get_db)):
    from src.models import Product, Review as ProductReview
    from src.services.catalog import sellable_facts
    product = await db.scalar(select(Product).where(Product.product_id == product_id,
        Product.tenant_id == actor.tenant_id, Product.shop_id.in_(actor.shop_ids), Product.status == "on_sale"))
    if product is None:
        raise NotFound("商品不存在或不在授权范围")
    rows = (await db.execute(select(ProductReview.rating, ProductReview.content).where(
        ProductReview.product_id == product_id, ProductReview.tenant_id == actor.tenant_id,
        ProductReview.shop_id == product.shop_id).order_by(ProductReview.id).limit(200))).all()
    return response(request, {"product_id": product.product_id, "title": product.title,
        "brand": product.brand, "category": product.category, "description": product.description,
        "attributes": product.attributes or {}, "source": "sandbox_catalog",
        "available_offer": await sellable_facts(db, actor, product_ids=[product_id]),
        "reviews": {"sample_count": len(rows), "samples": [row.content for row in rows[:3]],
                    "average": round(sum(row.rating for row in rows)/len(rows),2) if rows else None,
                    "note": "最多 200 条评价样本；用户反馈不能替代商品参数"}})


@router.post("/after-sales/drafts", status_code=201)
async def draft(payload: Draft, request: Request, actor: ActorScope = Depends(get_current_actor), db: AsyncSession = Depends(get_db)):
    case = await create_draft(db, actor, payload, request.state.request_id)
    await db.commit()
    return response(request, case_data(case))


@router.get("/after-sales")
async def cases(request: Request, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                actor: ActorScope = Depends(require_roles("buyer", "staff")), db: AsyncSession = Depends(get_db)):
    from src.models import AfterSaleCase
    query = select(AfterSaleCase).where(AfterSaleCase.tenant_id == actor.tenant_id)
    query = query.where(AfterSaleCase.buyer_id == actor.user_id) if actor.is_buyer else query.where(AfterSaleCase.shop_id.in_(actor.shop_ids))
    records = (await db.scalars(query.order_by(AfterSaleCase.updated_at.desc(), AfterSaleCase.id).limit(limit).offset(offset))).all()
    return response(request, {"items": [case_data(c) for c in records], "limit": limit, "offset": offset})


@router.post("/after-sales/{case_id}/cancel")
async def cancel(case_id: UUID, payload: CancelCase, request: Request,
                 actor: ActorScope = Depends(require_roles("buyer")), db: AsyncSession = Depends(get_db)):
    record = await cancel_case(db, actor, case_id, payload, request.state.request_id)
    await db.commit()
    return response(request, case_data(record))


@router.post("/after-sales/{case_id}/supplement")
async def supplement(case_id: UUID, payload: SupplementCase, request: Request,
                     actor: ActorScope = Depends(require_roles("buyer")), db: AsyncSession = Depends(get_db)):
    record = await supplement_case(db, actor, case_id, payload, request.state.request_id)
    await db.commit()
    return response(request, case_data(record))


@router.get("/after-sales/{case_id}")
async def case(case_id: UUID, request: Request, actor: ActorScope = Depends(get_current_actor), db: AsyncSession = Depends(get_db)):
    return response(request, case_data(await get_case(db, actor, case_id)))


@router.post("/after-sales/{case_id}/submit")
async def submit(case_id: UUID, payload: Submit, request: Request, idempotency_key: str = Header(alias="Idempotency-Key"),
                 actor: ActorScope = Depends(get_current_actor), db: AsyncSession = Depends(get_db)):
    case = await submit_case(db, actor, case_id, payload, idempotency_key, request.state.request_id)
    await db.commit()
    return response(request, case_data(case))


@router.post("/after-sales/{case_id}/review")
async def review(case_id: UUID, payload: Review, request: Request,
                 actor: ActorScope = Depends(require_roles("staff")), db: AsyncSession = Depends(get_db)):
    case = await review_case(db, actor, case_id, payload, request.state.request_id)
    await db.commit()
    return response(request, case_data(case))


@router.get("/jobs/{job_id}")
async def job(job_id: str, request: Request, actor: ActorScope = Depends(get_current_actor), db: AsyncSession = Depends(get_db)):
    record = await db.scalar(select(Job).where(Job.job_id == job_id, Job.tenant_id == actor.tenant_id))
    if record is None or (record.payload or {}).get("owner_id") != actor.user_id:
        raise NotFound("任务不存在")
    return response(request, {"job_id": record.job_id, "status": record.status, "result": record.result, "error": record.error})


@router.post("/jobs/{job_id}/retry")
async def replay_job(job_id: str, request: Request,
                     actor: ActorScope = Depends(get_current_actor), db: AsyncSession = Depends(get_db)):
    from src.services.jobs import retry_job
    record = await retry_job(db, actor, job_id, request.state.request_id)
    await db.commit()
    return response(request, {"job_id": record.job_id, "status": record.status})


@router.get("/analytics/metrics")
async def metrics(request: Request, actor: ActorScope = Depends(require_roles("ops"))):
    from src.modules.analytics.metrics import get_metric_schema
    return response(request, get_metric_schema())


@router.post("/analytics/query")
async def analytics(payload: AnalyticsQuery, request: Request, actor: ActorScope = Depends(require_roles("ops")), db: AsyncSession = Depends(get_db)):
    from src.nl2sql.generator import generate_sql
    from src.nl2sql.executor import execute_readonly
    generated = await generate_sql(payload.question, actor=actor, semantic_plan=payload.semantic_plan)
    if not generated.ok:
        raise AppError(generated.error)
    result = await execute_readonly(generated.sql, actor=actor)
    query_id = uuid4().hex
    db.add(AuditLog(tenant_id=actor.tenant_id, actor_id=actor.user_id, action="analytics.query",
                    resource_type="analytics", resource_id=query_id, trace_id=request.state.request_id,
                    created_at=datetime.now(timezone.utc), detail={"sql": generated.normalized_sql,
                    "metric_ids": generated.metric_ids, "row_count": result.row_count}))
    await db.commit()
    return response(request, {"query_id": query_id, **asdict(result), "metric_ids": generated.metric_ids,
                              "explanation": generated.explanation, "assumptions": generated.assumptions,
                              "data_freshness": "分析投影同步快照" if result.data_as_of else "未记录完整授权范围的同步水位，请重新执行 analytics_sync"})


def conversation_scope(actor):
    filters = [Conversation.tenant_id == actor.tenant_id]
    if actor.is_buyer or actor.is_ops:
        return [*filters, Conversation.user_id == actor.user_id]
    if actor.is_staff:
        return [*filters, Conversation.takeover_by == actor.user_id]
    raise PermissionDenied("当前角色没有会话权限")


@router.get("/conversations")
async def conversations(request: Request, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                        actor: ActorScope = Depends(get_current_actor), db: AsyncSession = Depends(get_db)):
    records = (await db.scalars(select(Conversation).where(*conversation_scope(actor))
        .order_by(Conversation.updated_at.desc(), Conversation.id).limit(limit).offset(offset))).all()
    return response(request, {"items": [{"id": str(c.id), "title": c.title, "takeover_status": c.takeover_status,
        "takeover_by": c.takeover_by, "last_message_at": c.last_message_at} for c in records],
        "limit": limit, "offset": offset})


@router.delete("/conversations/{conversation_id}")
async def remove_conversation(conversation_id: UUID, request: Request,
        actor: ActorScope = Depends(require_roles("buyer", "ops")), db: AsyncSession = Depends(get_db)):
    from src.services.chat import delete_conversation
    await delete_conversation(db, actor, conversation_id, request.state.request_id)
    await db.commit()
    return response(request, {"id": str(conversation_id), "deleted": True})


@router.get("/conversations/{conversation_id}/messages")
async def messages(conversation_id: UUID, request: Request, before: int | None = Query(None, ge=1),
                   limit: int = Query(50, ge=1, le=100), actor: ActorScope = Depends(get_current_actor),
                   db: AsyncSession = Depends(get_db)):
    record = await db.scalar(select(Conversation).where(Conversation.id == conversation_id, *conversation_scope(actor)))
    if record is None:
        raise NotFound("会话不存在")
    query = select(Message).where(Message.conversation_id == record.id, Message.tenant_id == actor.tenant_id)
    if before is not None:
        query = query.where(Message.seq < before)
    rows = list((await db.scalars(query.order_by(Message.seq.desc()).limit(limit + 1))).all())
    more = len(rows) > limit
    records = list(reversed(rows[:limit]))
    return response(request, {"items": [{"id": str(m.id), "seq": m.seq, "role": m.role,
        "content": m.content, "meta": m.meta, "created_at": m.created_at} for m in records],
        "next_before": records[0].seq if more and records else None,
        "takeover_status": record.takeover_status, "takeover_by": record.takeover_by})


@router.post("/conversations/{conversation_id}/escalate")
async def escalate(conversation_id: UUID, payload: EscalateConversation, request: Request,
                   actor: ActorScope = Depends(require_roles("buyer")), db: AsyncSession = Depends(get_db)):
    from src.services.orders import owned_item
    record = await own_conversation(db, actor, conversation_id, lock=True)
    _, order = await owned_item(db, actor, payload.order_item_id)
    staff = list((await db.scalars(select(User).where(User.tenant_id == actor.tenant_id,
        User.role == "staff", User.is_active.is_(True)).order_by(User.id).with_for_update())).all())
    candidates = [user for user in staff if order.shop_id in (user.shop_ids or [])
                  and set(actor.shop_ids).issubset(set(user.shop_ids or []))]
    if not candidates:
        raise Conflict("该店铺暂无可分配客服")
    if record.takeover_status == "human":
        return response(request, {"takeover_status": record.takeover_status, "takeover_by": record.takeover_by})
    # 历史可能引用其他店铺订单，整段会话只分配给覆盖全部关联店铺的客服。
    from src.models import OrderItem
    history = list((await db.scalars(select(Message).where(Message.conversation_id == record.id,
        Message.tenant_id == actor.tenant_id, Message.role == "user"))).all())
    item_ids = [UUID(m.meta["request"]["order_item_id"]) for m in history
                if (m.meta or {}).get("request", {}).get("order_item_id")]
    if item_ids:
        shops = set((await db.scalars(select(Order.shop_id).join(OrderItem, OrderItem.order_id == Order.id)
            .where(Order.tenant_id == actor.tenant_id, OrderItem.tenant_id == actor.tenant_id,
                   OrderItem.id.in_(item_ids)))).all())
        candidates = [user for user in candidates if shops.issubset(set(user.shop_ids or []))]
        if not candidates:
            raise Conflict("历史关联多个店铺，暂无覆盖全部范围的客服")
    # 同租户客服行按固定顺序加锁，串行化并发分配后再计算接管量。
    loads = dict((await db.execute(select(Conversation.takeover_by, func.count(Conversation.id))
        .where(Conversation.tenant_id == actor.tenant_id, Conversation.takeover_status == "human")
        .group_by(Conversation.takeover_by))).all())
    selected = min(candidates, key=lambda user: (loads.get(str(user.id), 0), str(user.id)))
    record.takeover_by, record.takeover_status = str(selected.id), "human"
    from src.services.handoff import build_handoff_summary
    recent = list((await db.scalars(select(Message).where(Message.conversation_id == record.id,
        Message.tenant_id == actor.tenant_id).order_by(Message.seq.desc()).limit(6))).all())
    summary = build_handoff_summary(recent, order, payload.order_item_id)
    db.add(AuditLog(tenant_id=actor.tenant_id, actor_id=actor.user_id, action="conversation.escalated",
        resource_type="conversation", resource_id=str(record.id),
        detail={"shop_id": order.shop_id, "staff_id": record.takeover_by, "summary": summary},
        created_at=datetime.now(timezone.utc), trace_id=request.state.request_id))
    await db.commit()
    return response(request, {"takeover_status": record.takeover_status, "takeover_by": record.takeover_by})


@router.get("/conversations/{conversation_id}/handoff")
async def handoff_summary(conversation_id: UUID, request: Request,
                          actor: ActorScope = Depends(require_roles("staff")), db: AsyncSession = Depends(get_db)):
    record = await db.scalar(select(Conversation).where(Conversation.id == conversation_id,
        *conversation_scope(actor)))
    if record is None:
        raise NotFound("会话不存在或未分配给当前客服")
    audit = await db.scalar(select(AuditLog).where(AuditLog.tenant_id == actor.tenant_id,
        AuditLog.resource_type == "conversation", AuditLog.resource_id == str(record.id),
        AuditLog.action == "conversation.escalated").order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(1))
    return response(request, {"summary": (audit.detail or {}).get("summary") if audit else None})


@router.post("/conversations/{conversation_id}/reply")
async def staff_reply(conversation_id: UUID, payload: StaffReply, request: Request,
                      actor: ActorScope = Depends(require_roles("staff")), db: AsyncSession = Depends(get_db)):
    record = await db.scalar(select(Conversation).where(Conversation.id == conversation_id,
        *conversation_scope(actor)).with_for_update())
    if record is None:
        raise NotFound("会话不存在或未分配给当前客服")
    if record.takeover_status != "human":
        raise Conflict("请先接管会话再回复")
    content = payload.content.strip()
    if not content:
        raise AppError("回复不能为空")
    seq = (await db.scalar(select(func.max(Message.seq)).where(Message.conversation_id == record.id)) or 0) + 1
    db.add(Message(tenant_id=actor.tenant_id, conversation_id=record.id, seq=seq, role="staff",
        content=content, meta={"staff_id": actor.user_id}, trace_id=request.state.request_id))
    record.last_message_at = datetime.now(timezone.utc)
    await db.commit()
    return response(request, {"seq": seq, "content": content, "role": "staff"})


@router.post("/conversations", status_code=201)
async def conversation(request: Request, actor: ActorScope = Depends(get_current_actor), db: AsyncSession = Depends(get_db)):
    record = Conversation(tenant_id=actor.tenant_id, user_id=actor.user_id)
    db.add(record)
    await db.commit()
    return response(request, {"id": str(record.id)})


@router.post("/conversations/{conversation_id}/takeover")
async def takeover(conversation_id: UUID, payload: Takeover, request: Request,
                   actor: ActorScope = Depends(require_roles("staff")), db: AsyncSession = Depends(get_db)):
    # 会话当前没有 shop_id，避免把整租户会话错误授予任意店铺客服。
    # 只允许已明确分配给该客服的会话；分配由服务端管理流程完成。
    record = await db.scalar(select(Conversation).where(Conversation.id == conversation_id,
        Conversation.tenant_id == actor.tenant_id, Conversation.takeover_by == actor.user_id).with_for_update())
    if record is None:
        raise NotFound("会话不存在或未分配给当前客服")
    record.takeover_status = "human" if payload.active else "auto"
    db.add(AuditLog(tenant_id=actor.tenant_id, actor_id=actor.user_id, action="conversation.takeover",
                    resource_type="conversation", resource_id=str(record.id), detail={"active": payload.active},
                    created_at=datetime.now(timezone.utc), trace_id=request.state.request_id))
    await db.commit()
    return response(request, {"takeover_status": record.takeover_status})


@router.post("/chat/approve")
async def approve_chat(payload: ChatApproval, request: Request,
                       actor: ActorScope = Depends(require_roles("buyer", "ops")),
                       db: AsyncSession = Depends(get_db)):
    """确认咨询建议；仅恢复本人线程，不执行售后建单或退款。"""
    from src.services.chat import approve_suggestion
    final = await approve_suggestion(db, actor, payload, request.state.request_id)
    await db.commit()
    return response(request, final)


@router.post("/chat/stream")
async def chat(payload: Chat, request: Request, actor: ActorScope = Depends(get_current_actor), db: AsyncSession = Depends(get_db)):
    from src.api.responses import sse_event
    from src.services.chat import stream_chat

    record = await own_conversation(db, actor, payload.conversation_id)
    if record.takeover_status == "human":
        raise Conflict("会话已由客服接管")
    await db.rollback()
    trace_id = request.state.request_id

    async def events():
        async with aclosing(stream_chat(payload, actor, trace_id)) as stream:
            async for name, data in stream:
                yield sse_event(name, data, trace_id)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
