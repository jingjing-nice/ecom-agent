"""自动审核的真实 SQL 闭环；PostgreSQL 锁竞争另需集成验证。"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.models import (AfterSaleCase, AuditLog, EvidenceObject, Job, Order, OrderItem,
    OutboxEvent, PolicyDocument, PolicyVersion, RefundItem)
from src.modules.aftersale.automation import AutomaticPolicy
from src.services.automatic_aftersale import enqueue_review
from src.worker import _automatic_case_review, _create_case
from tests.test_business_completion import json_type, make_case


@pytest.fixture
async def automatic_db():
    engine = create_engine('sqlite://')
    for model in (Order, OrderItem, AfterSaleCase, AuditLog, EvidenceObject, Job,
                  OutboxEvent, PolicyDocument, PolicyVersion, RefundItem):
        model.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as session:
        db = SimpleNamespace(add=session.add, scalar=AsyncMock(side_effect=session.scalar),
            scalars=AsyncMock(side_effect=session.scalars), execute=AsyncMock(side_effect=session.execute),
            flush=AsyncMock(side_effect=session.flush))
        case = make_case(session, status='SUBMITTED')
        case.case_type = 'refund'
        doc = PolicyDocument(tenant_id='demo', doc_id='refund', title='退款政策', scope='after_sale')
        session.add(doc); session.flush()
        policy = PolicyVersion(tenant_id='demo', policy_document_id=doc.id, scope='after_sale', version='v1',
            status='published', effective_from=datetime.now(timezone.utc)-timedelta(days=1),
            automation_rules={'rules':[{'case_type':'refund', 'order_statuses':['paid'],
                'max_days_since_payment':7, 'max_amount_minor':50000, 'require_evidence':True}]})
        session.add(policy); session.commit()
        yield db, session, case, policy
    engine.dispose()


def add_evidence(session, case, *, source='ocr', amount=100):
    order = session.get(Order, case.order_id)
    evidence = EvidenceObject(tenant_id='demo', owner_id='buyer', kind='refund_evidence',
        object_key='private/test.jpg', status='extracted', extracted={'fields':[
            {'field_name':'order_no','value':order.order_no,'status':'recognized','source':source},
            {'field_name':'amount_minor','value':amount,'status':'recognized','source':source}]})
    session.add(evidence); session.flush()
    case.evidence_ids = [str(evidence.id)]
    session.commit()


async def run_review(db, session, case):
    job = enqueue_review(db, case); session.commit()
    await _automatic_case_review(db, job); session.commit()
    return job


async def test_automatic_approval_creates_exactly_one_external_job(automatic_db):
    db, session, case, _ = automatic_db
    add_evidence(session, case)
    job = await run_review(db, session, case)
    assert case.status == 'APPROVED' and case.reviewer_id == 'system:automatic-policy-v1'
    assert session.query(OutboxEvent).count() == 1
    await _automatic_case_review(db, job); session.commit()
    assert session.query(OutboxEvent).count() == 1
    external = session.query(Job).filter_by(job_type='create_sandbox_case').one()
    await _create_case(db, external); session.commit()
    assert case.status == 'CREATED' and case.external_case_id.startswith('SANDBOX-')
    assert session.query(OutboxEvent).count() == 1
    assert session.query(AuditLog).filter_by(action='case.automatically_reviewed').count() == 2


@pytest.mark.parametrize('source,amount', [('ocr',99),('manual',100)])
async def test_unmatched_or_manually_entered_evidence_requests_more_info(automatic_db, source, amount):
    db, session, case, _ = automatic_db
    add_evidence(session, case, source=source, amount=amount)
    await run_review(db, session, case)
    assert case.status == 'NEED_MORE_INFO'
    assert session.query(OutboxEvent).count() == 0


async def test_revoked_policy_before_execution_blocks_external_write(automatic_db):
    db, session, case, policy = automatic_db
    add_evidence(session, case)
    await run_review(db, session, case)
    policy.status = 'revoked'; session.commit()
    external = session.query(Job).filter_by(job_type='create_sandbox_case').one()
    await _create_case(db, external); session.commit()
    assert case.status == 'NEED_MORE_INFO' and not case.external_case_id
    assert external.result['case_status'] == 'NEED_MORE_INFO'


async def test_duplicate_case_is_rejected_after_first_approval(automatic_db):
    db, session, first, _ = automatic_db
    add_evidence(session, first)
    await run_review(db, session, first)
    second = AfterSaleCase(tenant_id='demo', shop_id='a', case_no='second', order_id=first.order_id,
        order_item_id=first.order_item_id, buyer_id='buyer', actor_id='buyer', case_type='refund',
        reason='重复申请', status='SUBMITTED', version=1, idempotency_key='second', evidence_ids=first.evidence_ids)
    session.add(second); session.commit()
    await run_review(db, session, second)
    assert second.status == 'REJECTED'
    assert session.query(OutboxEvent).count() == 1


async def test_cancelled_case_skips_queued_review(automatic_db):
    db, session, case, _ = automatic_db
    enqueue_review(db, case); session.commit()
    job = session.query(Job).one()
    case.status, case.version = 'CANCELLED', 2; session.commit()
    await _automatic_case_review(db, job)
    assert case.status == 'CANCELLED' and job.result['skipped']


def test_duplicate_rules_and_unknown_actions_are_rejected():
    rule = {'case_type':'refund','order_statuses':['paid'],'max_days_since_payment':7,'max_amount_minor':100}
    with pytest.raises(ValidationError):
        AutomaticPolicy.model_validate({'rules':[rule,rule]})
    with pytest.raises(ValidationError):
        AutomaticPolicy.model_validate({'rules':[{**rule,'execute_arbitrary_sql':True}]})


async def test_supplement_automatically_requeues_and_approves(automatic_db):
    from src.api.schemas import SupplementCase
    from src.core.deps import ActorScope
    from src.services.aftersale import supplement_case
    db, session, case, _ = automatic_db
    await run_review(db, session, case)
    assert case.status == 'NEED_MORE_INFO'
    add_evidence(session, case)
    await supplement_case(db, ActorScope('buyer','demo','buyer',['a']), case.id,
        SupplementCase(expected_version=case.version, comment='已重新上传清晰原图'), 'trace')
    session.commit()
    pending = session.query(Job).filter_by(job_type='automatic_case_review',status='pending').one()
    await _automatic_case_review(db, pending); session.commit()
    assert case.status == 'APPROVED'
    assert session.query(OutboxEvent).count() == 1


@pytest.mark.parametrize('change,expected', [('expired','REJECTED'),('shipped','REJECTED'),
    ('missing_policy','NEED_MORE_INFO'),('conflict','NEED_MORE_INFO'),('foreign_policy','NEED_MORE_INFO')])
async def test_policy_and_order_constraints_block_automatic_writes(automatic_db, change, expected):
    db, session, case, policy = automatic_db
    add_evidence(session, case)
    order = session.get(Order, case.order_id)
    if change == 'expired':
        order.paid_at = datetime.now(timezone.utc)-timedelta(days=8)
    elif change == 'shipped':
        order.shipped_at = datetime.now(timezone.utc)
    elif change == 'missing_policy':
        policy.automation_rules = None
    elif change == 'foreign_policy':
        policy.tenant_id = 'other'
    else:
        session.add(PolicyVersion(tenant_id='demo',policy_document_id=policy.policy_document_id,
            scope='after_sale',version='conflict',status='published',effective_from=policy.effective_from,
            automation_rules=policy.automation_rules))
    session.commit()
    await run_review(db, session, case)
    assert case.status == expected and session.query(OutboxEvent).count() == 0


async def test_unknown_automatically_probes_then_retries_same_request(automatic_db, monkeypatch):
    from src.services.platform_adapter import FaultInjectingAdapter, SandboxPlatformAdapter
    db, session, case, _ = automatic_db
    add_evidence(session, case)
    await run_review(db, session, case)
    job = session.query(Job).filter_by(job_type='create_sandbox_case').one()
    adapter = FaultInjectingAdapter(SandboxPlatformAdapter(), faults=['timeout'], probe_result='missing')
    monkeypatch.setattr('src.worker.get_platform_adapter',lambda:adapter)
    await _create_case(db, job); session.commit()
    assert case.status == 'UNKNOWN'
    await _create_case(db, job); session.commit()
    assert case.status == 'APPROVED' and job.status == 'pending'
    await _create_case(db, job); session.commit()
    assert case.status == 'CREATED' and job.status == 'succeeded'
    assert case.external_case_id == 'SANDBOX-'+case.id.hex


async def test_worker_exception_rolls_back_business_state_and_retries(automatic_db, monkeypatch):
    from src import worker
    from src.services.platform_adapter import SandboxPlatformAdapter
    db, session, case, _ = automatic_db
    add_evidence(session, case)
    await run_review(db, session, case)
    job = session.query(Job).filter_by(job_type='create_sandbox_case').one()
    session.commit()

    class Context:
        def __init__(self, transaction=None):
            self.transaction = transaction
        async def __aenter__(self):
            if self.transaction:
                self.transaction.__enter__()
            return db
        async def __aexit__(self, *exc):
            if self.transaction:
                return self.transaction.__exit__(*exc)
            if exc[0]:
                session.rollback()
            else:
                session.commit()
            return False

    db.begin_nested = lambda: Context(session.begin_nested())
    db.refresh = AsyncMock(side_effect=session.refresh)
    monkeypatch.setattr(worker,'SessionFactory',SimpleNamespace(begin=lambda:Context()))
    adapter = SandboxPlatformAdapter()
    adapter.create_case = AsyncMock(side_effect=RuntimeError('simulated adapter failure'))
    monkeypatch.setattr(worker,'get_platform_adapter',lambda:adapter)
    assert await worker.tick()
    session.refresh(case)
    assert case.status == 'APPROVED' and not case.external_case_id
    assert '稍后重试' in case.last_error
    assert job.status == 'pending' and job.attempts == 1 and job.next_retry_at
    assert session.query(OutboxEvent).one().status == 'pending'
    job.next_retry_at = None; session.commit()
    monkeypatch.setattr(worker,'get_platform_adapter',lambda:SandboxPlatformAdapter())
    assert await worker.tick()
    assert case.status == 'CREATED' and job.status == 'succeeded' and case.last_error is None
    assert job.attempts == 2 and job.error is None and job.next_retry_at is None
