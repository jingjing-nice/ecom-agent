"""新增业务入口使用真实 SQLite 查询和 FastAPI 鉴权依赖，验证角色及状态闭环。

PostgreSQL 行锁、RLS 和跨进程并发另需容器集成验证。
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from src.api.routes import router
from src.core.database import get_db
from src.core.deps import ActorScope, get_current_actor
from src.core.exceptions import register_exception_handlers
from src.models import AfterSaleCase, AuditLog, Conversation, EvidenceObject, Message, Order, OrderItem, User, Job


@compiles(JSONB, "sqlite")
def json_type(type_, compiler, **kwargs):
    return "JSON"


@pytest.fixture
async def app_client():
    engine = create_engine("sqlite://")
    for model in (Order, OrderItem, AfterSaleCase, EvidenceObject, AuditLog, User, Conversation, Message, Job):
        model.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as session:
        db = SimpleNamespace(add=session.add, scalar=AsyncMock(side_effect=session.scalar),
            scalars=AsyncMock(side_effect=session.scalars), execute=AsyncMock(side_effect=session.execute),
            commit=AsyncMock(side_effect=session.commit), flush=AsyncMock(side_effect=session.flush))
        actor = ActorScope("buyer", "demo", "buyer", ["a"])
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(router, prefix="/v1")
        @app.middleware("http")
        async def request_id(request, call_next):
            request.state.request_id = "test"
            return await call_next(request)
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_actor] = lambda: actor
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            def switch(role, user="buyer", tenant="demo", shops=None):
                nonlocal actor
                actor = ActorScope(user, tenant, role, shops if shops is not None else ["a"])
            yield client, session, switch
    engine.dispose()


def make_case(session, status="NEED_MORE_INFO", buyer="buyer", shop="a", tenant="demo"):
    order = Order(tenant_id=tenant, shop_id=shop, order_no=uuid4().hex, buyer_id=buyer,
                  status="paid", paid_at=datetime.now(timezone.utc), paid_amount_minor=100)
    session.add(order); session.flush()
    item = OrderItem(tenant_id=tenant, order_id=order.id, sku_id="sku", product_id="p1", quantity=1,
                     title_snapshot="吸顶灯", brand_snapshot="品牌", category_snapshot="吸顶灯", paid_amount_minor=100)
    session.add(item); session.flush()
    case = AfterSaleCase(tenant_id=tenant, shop_id=shop, case_no=uuid4().hex, order_id=order.id,
        order_item_id=item.id, buyer_id=buyer, actor_id=buyer, case_type="exchange", status=status,
        version=1, idempotency_key=uuid4().hex, reason="尺寸不合适")
    session.add(case); session.commit()
    return case


async def test_buyer_supplements_then_cancels_and_version_conflicts(app_client):
    client, session, _ = app_client
    case = make_case(session)
    result = await client.post(f"/v1/after-sales/{case.id}/supplement", json={"expected_version":1,"comment":"已补充问题说明"})
    assert result.status_code == 200, result.text
    assert result.json()["data"]["status"] == "REVIEWING"
    assert result.json()["data"]["version"] == 2
    result = await client.post(f"/v1/after-sales/{case.id}/cancel", json={"expected_version":1,"reason":"不再需要"})
    assert result.status_code == 409
    result = await client.post(f"/v1/after-sales/{case.id}/cancel", json={"expected_version":2,"reason":"不再需要"})
    assert result.status_code == 200
    assert result.json()["data"]["status"] == "CANCELLED"
    assert session.query(AuditLog).count() == 2


async def test_foreign_evidence_rejected_without_case_transition(app_client):
    client, session, _ = app_client
    case = make_case(session)
    evidence = EvidenceObject(tenant_id="other", owner_id="buyer", object_key="other/private.jpg", status="extracted")
    session.add(evidence); session.commit()
    result = await client.post(f"/v1/after-sales/{case.id}/supplement", json={"expected_version":1,"comment":"补图","evidence_ids":[str(evidence.id)]})
    assert result.status_code == 404
    assert case.status == "NEED_MORE_INFO" and case.version == 1


async def test_case_lists_filter_buyer_tenant_and_staff_shop(app_client):
    client, session, switch = app_client
    own = make_case(session)
    make_case(session, buyer="another", shop="b")
    make_case(session, tenant="other")
    result = await client.get('/v1/after-sales')
    assert [row['id'] for row in result.json()['data']['items']] == [str(own.id)]
    switch('staff','staff')
    result = await client.get('/v1/after-sales')
    assert [row['id'] for row in result.json()['data']['items']] == [str(own.id)]
    result = await client.post(f'/v1/after-sales/{own.id}/cancel',json={'expected_version':1,'reason':'取消'})
    assert result.status_code == 403


async def test_rejection_requires_structured_appeal_path(app_client):
    client, session, switch = app_client
    case = make_case(session,status="REVIEWING")
    switch('staff','staff')
    payload = {'expected_version':1,'decision':'REJECTED','comment':'缺少政策支持'}
    result = await client.post(f'/v1/after-sales/{case.id}/review',json=payload)
    assert result.status_code == 400
    result = await client.post(f'/v1/after-sales/{case.id}/review',json={**payload,'appeal_path':'联系店铺申请复核'})
    assert result.status_code == 200
    assert result.json()['data']['appeal_path'] == '联系店铺申请复核'


async def test_conversation_handoff_reply_release_and_access(app_client):
    client, session, switch = app_client
    case = make_case(session)
    staff = User(tenant_id="demo",email="staff@test",display_name="客服",password_hash="unused",role="staff",shop_ids=["a"])
    session.add(staff); session.commit()
    result = await client.post('/v1/conversations',json={})
    conv_id = result.json()['data']['id']
    result = await client.post(f'/v1/conversations/{conv_id}/escalate',json={'order_item_id':str(case.order_item_id)})
    assert result.status_code == 200, result.text
    switch('staff',str(staff.id))
    result = await client.get('/v1/conversations')
    assert result.json()['data']['items'][0]['id'] == conv_id
    result = await client.post(f'/v1/conversations/{conv_id}/reply',json={'content':'请补充凭证图片'})
    assert result.status_code == 200
    result = await client.post(f'/v1/conversations/{conv_id}/takeover',json={'active':False})
    assert result.status_code == 200
    result = await client.post(f'/v1/conversations/{conv_id}/reply',json={'content':'重复回复'})
    assert result.status_code == 409
    switch('buyer')
    result = await client.get(f'/v1/conversations/{conv_id}/messages')
    assert result.json()['data']['items'][0]['role'] == 'staff'
    switch('buyer','another')
    assert (await client.get(f'/v1/conversations/{conv_id}/messages')).status_code == 404
    switch('staff','unassigned')
    assert (await client.get(f'/v1/conversations/{conv_id}/messages')).status_code == 404


async def test_message_history_paginates_without_overlap(app_client):
    client, session, _ = app_client
    conv = Conversation(tenant_id='demo',user_id='buyer')
    session.add(conv); session.flush()
    for seq in range(1,6):
        session.add(Message(tenant_id='demo',conversation_id=conv.id,seq=seq,role='user',content=str(seq)))
    session.commit()
    result = (await client.get(f'/v1/conversations/{conv.id}/messages?limit=2')).json()['data']
    assert [m['seq'] for m in result['items']] == [4,5]
    result = (await client.get(f'/v1/conversations/{conv.id}/messages?limit=2&before={result["next_before"]}')).json()['data']
    assert [m['seq'] for m in result['items']] == [2,3]


async def test_failed_vision_job_can_be_retried_only_by_owner(app_client):
    from src.models import Job
    client, session, switch = app_client
    Job.__table__.create(session.bind, checkfirst=True)
    image = EvidenceObject(tenant_id='demo',owner_id='buyer',kind='refund_evidence',object_key='private.jpg',status='failed')
    session.add(image); session.flush()
    job = Job(tenant_id='demo',job_id='failed-vision',job_type='vision_extract',status='failed',attempts=5,
              payload={'owner_id':'buyer','image_id':str(image.id)},error='Provider unavailable')
    session.add(job); session.commit()
    switch('buyer','another')
    assert (await client.post('/v1/jobs/failed-vision/retry',json={})).status_code == 404
    switch('buyer')
    result = await client.post('/v1/jobs/failed-vision/retry',json={})
    assert result.status_code == 200, result.text
    assert job.status == 'pending' and job.attempts == 0 and image.status == 'extracting'
    assert job.error is None
    assert (await client.post('/v1/jobs/failed-vision/retry',json={})).status_code == 409


async def test_external_write_jobs_cannot_use_generic_replay(app_client):
    from src.models import Job
    client, session, _ = app_client
    Job.__table__.create(session.bind, checkfirst=True)
    session.add(Job(tenant_id='demo',job_id='external',job_type='create_sandbox_case',status='failed',payload={'owner_id':'buyer'}))
    session.commit()
    assert (await client.post('/v1/jobs/external/retry',json={})).status_code == 403


async def test_handoff_balances_active_load_and_keeps_private_snapshot(app_client):
    client, session, switch = app_client
    case = make_case(session)
    staff = [User(tenant_id='demo', email=f'agent{i}@test', display_name='客服',
        password_hash='unused', role='staff', shop_ids=['a']) for i in range(2)]
    session.add_all(staff); session.flush()
    session.add(Conversation(tenant_id='demo', user_id='another',
        takeover_by=str(staff[0].id), takeover_status='human'))
    conv = Conversation(tenant_id='demo', user_id='buyer')
    session.add(conv); session.flush()
    for seq in range(1, 9):
        session.add(Message(tenant_id='demo', conversation_id=conv.id, seq=seq,
            role='user', content=f'问题{seq}' + '长' * 600))
    session.commit()
    url = f'/v1/conversations/{conv.id}'
    result = await client.post(url+'/escalate', json={'order_item_id':str(case.order_item_id)})
    assert result.status_code == 200, result.text
    assert result.json()['data']['takeover_by'] == str(staff[1].id)
    assert (await client.get(url+'/handoff')).status_code == 403
    # 重复转人工不产生第二份交接快照。
    assert (await client.post(url+'/escalate', json={'order_item_id':str(case.order_item_id)})).status_code == 200
    assert session.query(AuditLog).filter_by(action='conversation.escalated').count() == 1
    switch('staff', str(staff[1].id))
    summary = (await client.get(url+'/handoff')).json()['data']['summary']
    assert summary['order_item_id'] == str(case.order_item_id)
    assert [m['seq'] for m in summary['recent_messages']] == list(range(3,9))
    assert all(len(m['excerpt']) == 500 for m in summary['recent_messages'])
    switch('staff', str(staff[0].id))
    assert (await client.get(url+'/handoff')).status_code == 404
    switch('staff', str(staff[1].id), tenant='other')
    assert (await client.get(url+'/handoff')).status_code == 404


async def test_ops_unified_order_tool_keeps_tenant_shop_boundaries(app_client):
    from src.agents.tools import AgentRuntime
    from src.api.schemas import Chat
    client, session, switch = app_client
    from src.models import ToolCallRecord
    ToolCallRecord.__table__.create(session.bind)
    allowed = make_case(session)
    make_case(session, buyer='another',shop='b')
    make_case(session,tenant='other')
    switch('ops','operator',shops=['a'])
    response = await client.get('/v1/orders')
    assert response.status_code == 200
    assert [o['id'] for o in response.json()['data']['items']] == [str(allowed.order_id)]
    rt = AgentRuntime(SimpleNamespace(add=session.add,scalars=AsyncMock(side_effect=session.scalars)),
        ActorScope('operator','demo','ops',['a']),
        Chat(conversation_id=uuid4(),client_message_id='orders',message='查询店铺订单'),'trace')
    result = await rt.invoke('aftersale',{'name':'list_orders','args':{},'id':'lookup','type':'tool_call'})
    assert result.artifact['kind'] == 'orders'
    assert [o['id'] for o in result.artifact['orders']] == [str(allowed.order_id)]
    denied = await rt.invoke('aftersale',{'name':'list_orders','args':{'order_no':'nonexistent'},'id':'none','type':'tool_call'})
    assert denied.artifact['orders'] == []
    # 统一读入口不授予运营账号售后写权限。
    assert (await client.post('/v1/after-sales/drafts',json={
        'order_item_id':str(allowed.order_item_id),'case_type':'refund','reason':'测试'})).status_code == 403


async def test_chat_approval_persists_and_replays_only_same_decision(app_client, monkeypatch):
    client, session, switch = app_client
    conversation = Conversation(tenant_id="demo", user_id="buyer")
    session.add(conversation); session.flush()
    request = {"conversation_id": str(conversation.id), "client_message_id": "approval-1", "message": "换货咨询"}
    paused = {"message": "咨询建议", "thread_id": "thread-1", "requires_human_approval": True}
    user = Message(tenant_id="demo", conversation_id=conversation.id, seq=1, role="user",
        content=request["message"], client_message_id=request["client_message_id"], meta={"request": request, "answer": paused})
    assistant = Message(tenant_id="demo", conversation_id=conversation.id, seq=2, role="assistant", content="咨询建议", meta=paused)
    session.add_all([user, assistant]); session.commit()
    final = {"message": "咨询建议", "thread_id": "thread-1", "approval": {"decision": "APPROVED", "by": "buyer"}}
    resume = AsyncMock(return_value=final)
    monkeypatch.setattr("src.agents.supervisor.Supervisor.resume", resume)
    payload = {"conversation_id": str(conversation.id), "client_message_id": "approval-1", "decision": "APPROVED"}
    switch("buyer", user="other")
    assert (await client.post('/v1/chat/approve', json=payload)).status_code == 404
    resume.assert_not_awaited()
    switch("buyer")
    assert (await client.post('/v1/chat/approve', json={**payload, "decision": "invalid"})).status_code == 422
    assert (await client.post('/v1/chat/approve', json=payload)).json()['data'] == final
    assert assistant.meta == final and user.meta['answer'] == final
    assert (await client.post('/v1/chat/approve', json=payload)).status_code == 200
    assert resume.await_count == 1
    assert (await client.post('/v1/chat/approve', json={**payload, "decision": "REJECTED"})).status_code == 409


async def test_chat_approval_resumes_real_graph_after_database_retry(app_client, monkeypatch):
    from src.agents.checkpoint import new_thread_id, thread_config
    from src.agents.supervisor import Supervisor
    from src.api.schemas import Chat
    from src.core.config import Settings
    from unittest.mock import MagicMock

    client, session, _ = app_client
    monkeypatch.setattr('src.agents.tools.get_settings', lambda: Settings(
        AGENT_ROUTING_MODE='rules', AGENT_HUMAN_APPROVAL=True, _env_file=None))
    conversation = Conversation(tenant_id='demo', user_id='buyer')
    session.add(conversation); session.flush()
    original = Chat(conversation_id=conversation.id, client_message_id='real-approval', message='咨询政策')
    actor = ActorScope('buyer', 'demo', 'buyer', ['a'])
    supervisor = Supervisor(MagicMock(), actor, 'test')
    thread = new_thread_id()
    graph, _ = supervisor._build(original, thread)
    initial = supervisor._initial_state(original)
    initial['answer'] = {'message': '政策咨询建议', 'pending_confirmation': True, 'cards': []}
    await graph.aupdate_state(thread_config(thread), initial, as_node='finish')
    state = await graph.ainvoke(None, thread_config(thread))
    paused = supervisor._answer(state, thread)
    user = Message(tenant_id='demo', conversation_id=conversation.id, seq=1, role='user',
        content=original.message, client_message_id=original.client_message_id,
        meta={'request': original.model_dump(mode='json'), 'answer': paused})
    assistant = Message(tenant_id='demo', conversation_id=conversation.id, seq=2, role='assistant',
        content=paused['message'], meta=paused)
    session.add_all([user, assistant]); session.commit()
    # 模拟图恢复成功，但 API 消息事务尚未提交，客户端随后重试。
    await supervisor.resume(original, thread, {'decision': 'REJECTED', 'by': 'buyer'})
    body = {'conversation_id': str(conversation.id), 'client_message_id': original.client_message_id, 'decision': 'REJECTED'}
    first = await client.post('/v1/chat/approve', json=body)
    assert first.status_code == 200
    assert first.json()['data']['thread_id'] == thread
    assert not first.json()['data']['pending_confirmation']
    assert not first.json()['data'].get('requires_human_approval')
    assert (await client.post('/v1/chat/approve', json=body)).json()['data'] == first.json()['data']
    assert assistant.meta['approval']['decision'] == 'REJECTED'
    assert session.query(AuditLog).filter_by(action='chat.approval').count() == 1


async def test_chat_service_persists_then_replays_without_running_agent(app_client, monkeypatch):
    from src.services.chat import _run_chat
    from src.api.schemas import Chat
    from src.core.exceptions import Conflict
    client, session, _ = app_client
    conversation = Conversation(tenant_id='demo', user_id='buyer')
    session.add(conversation); session.commit()
    db = SimpleNamespace(add=session.add, scalar=AsyncMock(side_effect=session.scalar),
        commit=AsyncMock(side_effect=session.commit))
    calls = []
    answer = {'message': '商品建议', 'cards': [], 'trace': [{'tool': 'search_products'}]}

    async def stream(self, payload):
        calls.append(payload)
        yield {'event': 'progress', 'data': answer['trace'][0]}
        yield {'event': 'delta', 'data': {'text': '商品建议'}}
        yield {'event': 'answer', 'data': answer}

    monkeypatch.setattr('src.agents.supervisor.Supervisor.stream', stream)
    payload = Chat(conversation_id=conversation.id, client_message_id='service-chat', message='推荐吸顶灯')
    actor = ActorScope('buyer', 'demo', 'buyer', ['a'])
    events = [item async for item in _run_chat(db, payload, actor, 'trace')]
    assert [name for name, _ in events] == ['workflow.progress', 'answer.delta', 'answer.completed']
    assert session.query(Message).count() == 2
    assert conversation.title == payload.message
    replay = [item async for item in _run_chat(db, payload, actor, 'retry')]
    assert replay[-1] == ('answer.completed', answer) and len(calls) == 1
    with pytest.raises(Conflict):
        _ = [item async for item in _run_chat(db, payload.model_copy(update={'message': '不同问题'}), actor, 'other')]
    assert session.query(Message).count() == 2


async def test_chat_service_rolls_back_partial_messages_on_agent_error(app_client, monkeypatch):
    from src.services.chat import stream_chat
    from src.api.schemas import Chat
    from src.core.exceptions import WorkflowError
    _, session, _ = app_client
    conversation = Conversation(tenant_id='demo', user_id='buyer')
    session.add(conversation); session.commit()
    payload = Chat(conversation_id=conversation.id, client_message_id='failed-stream', message='推荐吸顶灯')
    db = SimpleNamespace(add=session.add, scalar=AsyncMock(side_effect=session.scalar),
        commit=AsyncMock(side_effect=session.commit), rollback=AsyncMock(side_effect=session.rollback))

    class Context:
        async def __aenter__(self):
            return db
        async def __aexit__(self, *exc):
            session.rollback()

    async def stream(self, payload):
        yield {'event': 'progress', 'data': {'tool': 'search_products'}}
        raise WorkflowError('模型暂时不可用')

    monkeypatch.setattr('src.services.chat.SessionFactory', lambda: Context())
    monkeypatch.setattr('src.agents.supervisor.Supervisor.stream', stream)
    events = [item async for item in stream_chat(payload, ActorScope('buyer', 'demo', 'buyer', ['a']), 'trace')]
    assert events[0][0] == 'message.accepted' and events[-1][0] == 'error'
    assert events[-1][1]['retryable']
    assert session.query(Message).count() == 0
    db.commit.assert_not_awaited()


async def test_batch_refund_evidence_excludes_other_owners_and_invalid_images(app_client):
    from src.services.evidence import load_refund_evidence
    from src.services.aftersale import validate_evidence
    from src.core.exceptions import NotFound
    _, session, _ = app_client
    images = [EvidenceObject(tenant_id=tenant, owner_id=owner, kind=kind, status=status,
        file_name='proof.png', object_key=uuid4().hex, extracted={'fields': []})
        for tenant, owner, kind, status in [
            ('demo', 'buyer', 'refund_evidence', 'extracted'),
            ('demo', 'buyer', 'refund_evidence', 'extracted'),
            ('other', 'buyer', 'refund_evidence', 'extracted'),
            ('demo', 'other', 'refund_evidence', 'extracted'),
            ('demo', 'buyer', 'product_image', 'extracted'),
            ('demo', 'buyer', 'refund_evidence', 'extracting')]]
    session.add_all(images); session.commit()
    db = SimpleNamespace(scalars=AsyncMock(side_effect=session.scalars))
    actor = ActorScope('buyer', 'demo', 'buyer', ['a'])
    records = await load_refund_evidence(db, actor, [str(image.id) for image in images] + [images[0].id])
    assert {record.id for record in records} == {image.id for image in images[:2]}
    assert db.scalars.await_count == 1
    with pytest.raises(NotFound):
        await validate_evidence(db, actor, [images[0].id, images[3].id], None, None)


async def test_closing_chat_stream_closes_agent_and_database_session(app_client, monkeypatch):
    from src.services.chat import stream_chat
    from src.api.schemas import Chat
    _, session, _ = app_client
    conversation = Conversation(tenant_id='demo', user_id='buyer')
    session.add(conversation); session.commit()
    payload = Chat(conversation_id=conversation.id, client_message_id='closed-stream', message='推荐吸顶灯')
    db = SimpleNamespace(add=session.add, scalar=AsyncMock(side_effect=session.scalar), commit=AsyncMock())
    closed = []

    class Context:
        async def __aenter__(self):
            return db
        async def __aexit__(self, *exc):
            session.rollback()
            closed.append('database')

    async def agent(self, payload):
        try:
            yield {'event': 'progress', 'data': {'tool': 'search_products'}}
            raise AssertionError('关闭后不应继续执行')
        finally:
            closed.append('agent')

    monkeypatch.setattr('src.services.chat.SessionFactory', lambda: Context())
    monkeypatch.setattr('src.agents.supervisor.Supervisor.stream', agent)
    stream = stream_chat(payload, ActorScope('buyer', 'demo', 'buyer', ['a']), 'trace')
    assert (await anext(stream))[0] == 'message.accepted'
    assert (await anext(stream))[0] == 'workflow.progress'
    await stream.aclose()
    assert closed == ['agent', 'database']
    assert session.query(Message).count() == 0
    db.commit.assert_not_awaited()
