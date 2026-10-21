"""聊天事务与执行编排：归属、消息幂等、上下文继承、审批及增量事件。

只产出领域事件；HTTP/SSE 编码由 API 层负责。
"""
from datetime import datetime, timezone
from contextlib import aclosing
import traceback

from loguru import logger
from pydantic import ValidationError

from sqlalchemy import delete, func, select

from src.agents.supervisor import Supervisor
from src.api.schemas import Chat
from src.core.database import SessionFactory
from src.core.exceptions import AppError, Conflict, NotFound
from src.models import AuditLog, Message, Conversation
from src.services.chat_context import resolve_shopping_context


async def own_conversation(db, actor, conversation_id, lock=False):
    query = select(Conversation).where(Conversation.id == conversation_id, Conversation.tenant_id == actor.tenant_id,
                                       Conversation.user_id == actor.user_id)
    if lock:
        query = query.with_for_update()
    record = await db.scalar(query.execution_options(populate_existing=True))
    if record is None:
        raise NotFound("会话不存在")
    return record


async def delete_conversation(db, actor, conversation_id, trace_id):
    record = await own_conversation(db, actor, conversation_id, lock=True)
    await db.execute(delete(Message).where(
        Message.conversation_id == record.id, Message.tenant_id == actor.tenant_id))
    await db.delete(record)
    db.add(AuditLog(tenant_id=actor.tenant_id, actor_id=actor.user_id,
        action="conversation.deleted", resource_type="conversation", resource_id=str(record.id),
        trace_id=trace_id, detail={}, created_at=datetime.now(timezone.utc)))


async def approve_suggestion(db, actor, payload, trace_id):
    record = await own_conversation(db, actor, payload.conversation_id, lock=True)
    if record.takeover_status == "human":
        raise Conflict("会话已由客服接管")
    message = await db.scalar(select(Message).where(
        Message.conversation_id == record.id, Message.tenant_id == actor.tenant_id,
        Message.role == "user", Message.client_message_id == payload.client_message_id))
    meta = (message.meta or {}) if message else {}
    answer = meta.get("answer") or {}
    if not answer.get("thread_id"):
        raise NotFound("待确认任务不存在")
    decision = {"decision": payload.decision, "by": actor.user_id}
    if answer.get("approval"):
        if answer["approval"] != decision:
            raise Conflict("任务已经按另一决策处理")
        return answer
    assistant = await db.scalar(select(Message).where(
        Message.conversation_id == record.id, Message.tenant_id == actor.tenant_id,
        Message.seq == message.seq + 1, Message.role == "assistant"))
    if assistant is None:
        raise Conflict("回复记录缺失，无法确认")
    original = Chat.model_validate(meta.get("effective_request") or meta["request"])
    final = await Supervisor(db, actor, trace_id).resume(original, answer["thread_id"], decision)
    message.meta = {**meta, "answer": final}
    assistant.meta, assistant.content = final, final.get("reply") or final["message"]
    db.add(AuditLog(tenant_id=actor.tenant_id, actor_id=actor.user_id,
        action="chat.approval", resource_type="conversation", resource_id=str(record.id),
        trace_id=trace_id, detail={"thread_id": answer["thread_id"], **decision},
        created_at=datetime.now(timezone.utc)))
    return final


async def stream_chat(payload, actor, trace_id):
    yield "message.accepted", {"client_message_id": payload.client_message_id}
    async with SessionFactory() as session:
        try:
            async with aclosing(_run_chat(session, payload, actor, trace_id)) as stream:
                async for event in stream:
                    yield event
        except AppError as exc:
            await session.rollback()
            yield "error", {"message": exc.message, "error_code": exc.error_code, "retryable": exc.retryable}
        except Exception as exc:
            # 只记录错误类型/代码位置及校验字段，不把模型原文、参数值写入日志。
            frames = [(frame.name, frame.lineno) for frame in traceback.extract_tb(exc.__traceback__)[-8:]]
            fields = ([{'loc': error['loc'], 'type': error['type']} for error in
                       exc.errors(include_input=False, include_context=False)] if isinstance(exc, ValidationError) else [])
            logger.error("聊天执行异常 trace_id={} type={} frames={} fields={}",
                         trace_id, type(exc).__name__, frames, fields)
            await session.rollback()
            yield "error", {"message": "任务处理失败，请稍后重试", "error_code": "internal_error", "retryable": True}


async def _run_chat(session, payload, actor, trace_id):
    # 同一会话串行执行，接管操作在当前任务完成后生效。
    record = await own_conversation(session, actor, payload.conversation_id, lock=True)
    if record.takeover_status == "human":
        raise Conflict("会话已由客服接管")
    original = payload.model_dump(mode="json")
    existing = await session.scalar(select(Message).where(Message.conversation_id == record.id,
        Message.tenant_id == actor.tenant_id, Message.client_message_id == payload.client_message_id))
    if existing:
        if existing.content != payload.message or (existing.meta or {}).get("request") != original:
            raise Conflict("同一消息 ID 不能提交不同内容")
        answer = (existing.meta or {}).get("answer")
        if answer is None:
            raise Conflict("消息正在处理，请稍后查看")
        await session.commit()
        for progress in answer.get("trace", []):
            yield "workflow.progress", progress
        yield "answer.delta", {"text": answer.get("reply") or answer["message"]}
        yield "answer.completed", answer
        return

    seq = (await session.scalar(select(func.max(Message.seq)).where(Message.conversation_id == record.id)) or 0) + 1
    message = Message(tenant_id=actor.tenant_id, conversation_id=record.id, seq=seq, role="user",
        content=payload.message, client_message_id=payload.client_message_id,
        trace_id=trace_id, meta={"request": original})
    session.add(message)
    previous = await session.scalar(select(Message).where(Message.conversation_id == record.id,
        Message.tenant_id == actor.tenant_id, Message.role == "assistant")
        .order_by(Message.seq.desc()).limit(1))
    effective = resolve_shopping_context(payload, previous.meta if previous else None)
    answer = None
    async with aclosing(Supervisor(session, actor, trace_id).stream(effective)) as stream:
        async for item in stream:
            if item["event"] == "answer":
                answer = item["data"]
            else:
                yield {"progress": "workflow.progress", "delta": "answer.delta"}[item["event"]], item["data"]
    if answer is None:
        raise AppError("任务未返回结果")
    message.meta = {"request": original, "effective_request": effective.model_dump(mode="json"), "answer": answer}
    session.add(Message(tenant_id=actor.tenant_id, conversation_id=record.id, seq=seq+1, role="assistant",
        content=answer.get("reply") or answer["message"], meta=answer, trace_id=trace_id))
    record.last_message_at = datetime.now(timezone.utc)
    if not record.title:
        record.title = payload.message[:80]
    await session.commit()
    yield "answer.completed", answer
