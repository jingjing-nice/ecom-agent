import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
import pytest
from src.api.schemas import Submit, Review
from src.core.deps import ActorScope
from src.core.exceptions import AppError, Conflict, NotFound
from src.services.aftersale import submit_case, get_case, review_case

BUYER = ActorScope("buyer-a", "tenant-a", "buyer", ["shop-a"])
STAFF = ActorScope("staff-a", "tenant-a", "staff", ["shop-a"])


def database():
    db = MagicMock()
    db.execute = AsyncMock()
    db.scalar = AsyncMock()
    return db


async def test_submit_requires_explicit_confirmation():
    db = database()
    with pytest.raises(AppError):
        await submit_case(db, BUYER, uuid4(), Submit(expected_version=1, confirmed=False), "key", "trace")
    db.execute.assert_not_called()


async def test_same_request_replays_without_writing():
    db, case_id = database(), uuid4()
    payload = Submit(expected_version=1, confirmed=True)
    digest = hashlib.sha256(json.dumps({"case_id":str(case_id), **payload.model_dump()}, sort_keys=True).encode()).hexdigest()
    case = SimpleNamespace(request_hash=digest)
    db.scalar.return_value = case
    assert await submit_case(db, BUYER, case_id, payload, "key", "trace") is case
    db.add.assert_not_called()


async def test_reused_key_with_different_body_conflicts():
    db = database()
    db.scalar.return_value = SimpleNamespace(request_hash="different")
    with pytest.raises(Conflict):
        await submit_case(db, BUYER, uuid4(), Submit(expected_version=1, confirmed=True), "key", "trace")
    db.add.assert_not_called()


async def test_not_owned_case_is_not_found():
    db = database()
    db.scalar.return_value = None
    with pytest.raises(NotFound):
        await get_case(db, BUYER, uuid4())
    query = db.scalar.call_args.args[0]
    params = query.compile().params
    assert "tenant-a" in params.values()
    assert "buyer-a" in params.values()


async def test_review_conflicting_version_does_not_enqueue(monkeypatch):
    db = database()
    monkeypatch.setattr("src.services.aftersale.get_case", AsyncMock(return_value=SimpleNamespace(version=2)))
    with pytest.raises(Conflict):
        await review_case(db, STAFF, uuid4(), Review(expected_version=1, decision="APPROVED", comment="核验", policy_version="v1"), "trace")
    db.add.assert_not_called()


async def test_approval_requires_valid_published_policy(monkeypatch):
    db = database()
    case = SimpleNamespace(version=1, status="REVIEWING")
    monkeypatch.setattr("src.services.aftersale.get_case", AsyncMock(return_value=case))
    db.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: []))
    with pytest.raises(AppError):
        await review_case(db, STAFF, uuid4(), Review(expected_version=1, decision="APPROVED", comment="核验", policy_version="missing"), "trace")
    db.add.assert_not_called()


async def test_confirmed_submit_enqueues_one_automatic_review_and_replays(monkeypatch):
    from src.models import Job
    from src.core.config import Settings
    monkeypatch.setattr('src.core.config.get_settings',lambda:Settings(AFTERSALE_AUTO_REVIEW=True,_env_file=None))
    db, case_id = database(), uuid4()
    case = SimpleNamespace(id=case_id, tenant_id=BUYER.tenant_id, buyer_id=BUYER.user_id,
        status='DRAFT', version=1)
    db.scalar.return_value = None
    monkeypatch.setattr('src.services.aftersale.get_case',AsyncMock(return_value=case))
    payload = Submit(expected_version=1,confirmed=True)
    await submit_case(db,BUYER,case_id,payload,'confirmed-key','trace')
    jobs = [call.args[0] for call in db.add.call_args_list if isinstance(call.args[0],Job)]
    assert len(jobs) == 1 and jobs[0].job_type == 'automatic_case_review'
    assert jobs[0].payload['expected_version'] == case.version == 2
    db.scalar.return_value = case
    db.add.reset_mock()
    assert await submit_case(db,BUYER,case_id,payload,'confirmed-key','trace') is case
    db.add.assert_not_called()
