"""工单适配故障注入与队列退避（方案 §5/§8）。

覆盖：
- plan_after_create / plan_after_probe：超时/UNKNOWN 与无幂等平台的处置计划
- 沙箱适配器幂等创建；重复投递返回同一沙箱单号
- Worker tick：超时注入 → UNKNOWN + 退避；UNKNOWN 请求号核对 → CREATED / 人工分支
- 重试指数退避 + 抖动与超限终态失败；next_retry_at 到期前不可领取
- 知识索引任务：keyword-only 降级与向量写回
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src import worker
from src.core.config import Settings
from src.core.exceptions import AppError, WorkflowError
from src.services.platform_adapter import (
    FaultInjectingAdapter,
    SandboxPlatformAdapter,
    plan_after_create,
    plan_after_probe,
    stable_request_no,
)


def make_job(job_type, payload, *, attempts=0, max_attempts=5):
    return SimpleNamespace(job_type=job_type, job_id="job-1", tenant_id="demo", payload=payload,
                           status="pending", attempts=attempts, max_attempts=max_attempts,
                           next_retry_at=None, locked_by=None, locked_at=None,
                           error=None, result=None, finished_at=None)


def make_case(status="APPROVED", *, version=1, external_case_id=None):
    return SimpleNamespace(id=uuid4(), status=status, version=version,
                           external_case_id=external_case_id, last_error=None)


def patch_worker_db(monkeypatch, scalars_sequence, *, query_result=None):
    """把 worker 的 SessionFactory 替换为内存桩；scalar 按调用序列返回。"""
    db = MagicMock()
    db.flush = AsyncMock()
    db.scalar = AsyncMock(side_effect=scalars_sequence)
    db.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: query_result or []))

    class Context:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return False

    factory = MagicMock()
    factory.begin = MagicMock(return_value=Context())
    monkeypatch.setattr("src.worker.SessionFactory", factory)
    return db


def test_plan_after_create_covers_timeout_unknown_and_no_idempotency():
    assert plan_after_create("created", supports_idempotency=True) == {
        "case_status": "CREATED", "retry": False, "reason": ""}
    timeout = plan_after_create("timeout", supports_idempotency=True)
    assert timeout["case_status"] == "UNKNOWN" and timeout["retry"]
    unknown = plan_after_create("unknown", supports_idempotency=False)
    assert unknown["case_status"] == "UNKNOWN" and "不支持幂等" in unknown["reason"]


def test_plan_after_probe_returns_created_or_human_review():
    created = plan_after_probe("created", supports_idempotency=True)
    assert created["case_status"] == "CREATED" and not created["retry"]
    missing = plan_after_probe("missing", supports_idempotency=True)
    assert missing["case_status"] == "REVIEWING" and "重新审批" in missing["reason"]
    human = plan_after_probe("missing", supports_idempotency=False)
    assert human["case_status"] == "REVIEWING" and "人工核对" in human["reason"]
    assert plan_after_probe("timeout", supports_idempotency=True)["retry"] is True


def test_retry_delay_backs_off_with_jitter_and_caps():
    assert 1 <= worker.retry_delay_seconds(1) <= 2
    assert 2 <= worker.retry_delay_seconds(2) <= 4
    assert 150 <= worker.retry_delay_seconds(20) <= 300  # 封顶 300 秒，附加抖动


async def test_worker_creates_case_with_stable_external_id(monkeypatch):
    monkeypatch.setattr("src.worker.get_platform_adapter", lambda: SandboxPlatformAdapter())
    case = make_case()
    job = make_job("create_sandbox_case", {"case_id": str(case.id)})
    patch_worker_db(monkeypatch, [job, case, None])
    assert await worker.tick() is True
    assert case.status == "CREATED" and case.version == 3  # APPROVED→CREATING→CREATED
    assert case.external_case_id == "SANDBOX-" + case.id.hex
    assert job.status == "succeeded" and job.result["external_case_id"] == case.external_case_id
    assert job.locked_by and job.locked_at


async def test_worker_timeout_injection_moves_case_to_unknown_with_backoff(monkeypatch):
    adapter = FaultInjectingAdapter(SandboxPlatformAdapter(), faults=["timeout"])
    monkeypatch.setattr("src.worker.get_platform_adapter", lambda: adapter)
    case = make_case()
    job = make_job("create_sandbox_case", {"case_id": str(case.id)})
    patch_worker_db(monkeypatch, [job, case, None])
    await worker.tick()
    assert case.status == "UNKNOWN" and case.version == 3
    assert job.status == "pending" and job.next_retry_at is not None
    assert "结果未知" in case.last_error


async def test_worker_probes_unknown_request_number_and_returns_created(monkeypatch):
    adapter = FaultInjectingAdapter(SandboxPlatformAdapter(), faults=[], probe_result="created")
    monkeypatch.setattr("src.worker.get_platform_adapter", lambda: adapter)
    case = make_case(status="UNKNOWN", version=2)
    job = make_job("create_sandbox_case", {"case_id": str(case.id)})
    patch_worker_db(monkeypatch, [job, case, None])
    await worker.tick()
    assert case.status == "CREATED" and case.external_case_id == "SANDBOX-" + case.id.hex
    assert job.status == "succeeded" and job.result["probe"] == "created"


async def test_worker_unknown_without_idempotency_goes_to_manual_review(monkeypatch):
    adapter = FaultInjectingAdapter(SandboxPlatformAdapter(), faults=[],
                                    probe_result="missing", supports_idempotency=False)
    monkeypatch.setattr("src.worker.get_platform_adapter", lambda: adapter)
    case = make_case(status="UNKNOWN", version=2)
    job = make_job("create_sandbox_case", {"case_id": str(case.id)})
    patch_worker_db(monkeypatch, [job, case, None])
    await worker.tick()
    assert case.status == "REVIEWING" and case.version == 3
    assert "人工核对" in case.last_error
    assert job.status == "succeeded" and job.result["case_status"] == "REVIEWING"


async def test_worker_probe_timeout_keeps_unknown_and_retries(monkeypatch):
    adapter = FaultInjectingAdapter(SandboxPlatformAdapter(), faults=[], probe_result="timeout")
    monkeypatch.setattr("src.worker.get_platform_adapter", lambda: adapter)
    case = make_case(status="UNKNOWN", version=2)
    job = make_job("create_sandbox_case", {"case_id": str(case.id)})
    patch_worker_db(monkeypatch, [job, case, None])
    await worker.tick()
    assert case.status == "UNKNOWN" and case.version == 2  # 无迁移，等待下一次核对
    assert job.status == "pending" and job.next_retry_at is not None


async def test_worker_retry_exhaustion_fails_job_terminally(monkeypatch):
    adapter = FaultInjectingAdapter(SandboxPlatformAdapter(), faults=["unknown"])
    monkeypatch.setattr("src.worker.get_platform_adapter", lambda: adapter)
    case = make_case()
    job = make_job("create_sandbox_case", {"case_id": str(case.id)}, attempts=4, max_attempts=5)
    patch_worker_db(monkeypatch, [job, case, None])
    await worker.tick()
    assert job.attempts == 5 and job.status == "failed" and job.finished_at is not None


async def test_worker_replays_created_case_without_new_external_call(monkeypatch):
    def unexpected():
        raise AssertionError("重复投递不应再次调用外部平台")

    monkeypatch.setattr("src.worker.get_platform_adapter", unexpected)
    case = make_case(status="CREATED", version=3, external_case_id="SANDBOX-stable")
    job = make_job("create_sandbox_case", {"case_id": str(case.id)})
    patch_worker_db(monkeypatch, [job, case, None])
    await worker.tick()
    assert job.status == "succeeded" and job.result["external_case_id"] == "SANDBOX-stable"
    assert case.version == 3


async def test_knowledge_index_degrades_to_keyword_only_without_embedding_key(monkeypatch):
    monkeypatch.setattr("src.worker.get_settings", lambda: Settings(DASHSCOPE_API_KEY="", _env_file=None))
    version = SimpleNamespace(id=uuid4(), status="draft", index_version=None)
    chunks = [SimpleNamespace(content="退货条款", embedding=None, model_version="")]
    job = make_job("knowledge_index", {"version_id": str(version.id)})
    patch_worker_db(monkeypatch, [job, version, None], query_result=chunks)
    await worker.tick()
    assert version.index_version == "keyword-only"
    assert chunks[0].model_version == "keyword-only" and chunks[0].embedding is None
    assert job.status == "succeeded" and job.result["note"].startswith("未配置 Embedding")


async def test_knowledge_index_writes_vectors_when_configured(monkeypatch):
    settings = Settings(DASHSCOPE_API_KEY="test", _env_file=None)
    monkeypatch.setattr("src.worker.get_settings", lambda: settings)
    gateway = MagicMock()
    gateway.embed = AsyncMock(return_value=[[0.5] * settings.EMBEDDING_DIM])
    monkeypatch.setattr("src.infra.model_gateway.get_model_gateway", lambda: gateway)
    version = SimpleNamespace(id=uuid4(), status="reviewing", index_version=None)
    chunks = [SimpleNamespace(content="退货条款", embedding=None, model_version="")]
    job = make_job("knowledge_index", {"version_id": str(version.id)})
    patch_worker_db(monkeypatch, [job, version, None], query_result=chunks)
    await worker.tick()
    assert chunks[0].embedding == [0.5] * settings.EMBEDDING_DIM
    assert chunks[0].model_version == settings.EMBEDDING_MODEL
    assert version.index_version == settings.EMBEDDING_MODEL
    assert job.status == "succeeded" and job.result["embedded"] == 1


async def test_knowledge_index_retries_when_embedding_service_fails(monkeypatch):
    settings = Settings(DASHSCOPE_API_KEY="test", _env_file=None)
    monkeypatch.setattr("src.worker.get_settings", lambda: settings)
    gateway = MagicMock()
    gateway.embed = AsyncMock(side_effect=WorkflowError("Embedding 服务不可用"))
    monkeypatch.setattr("src.infra.model_gateway.get_model_gateway", lambda: gateway)
    version = SimpleNamespace(id=uuid4(), status="draft", index_version=None)
    chunks = [SimpleNamespace(content="退货条款", embedding=None, model_version="")]
    job = make_job("knowledge_index", {"version_id": str(version.id)})
    patch_worker_db(monkeypatch, [job, version, None], query_result=chunks)
    await worker.tick()
    assert job.status == "pending" and job.next_retry_at is not None
    assert chunks[0].embedding is None and "索引构建失败" in job.error


async def test_vision_retryable_failure_backs_off_and_fatal_fails(monkeypatch):
    image_id, owner = str(uuid4()), "buyer-a"

    def evidence():
        return SimpleNamespace(id=uuid4(), object_key="k", file_name="f.png", kind="refund_evidence",
                               status="extracting", extracted=None, content_type="", size_bytes=0)

    from src.infra import storage
    monkeypatch.setattr(storage, "download_object", AsyncMock(side_effect=WorkflowError("模型调用失败")))
    job = make_job("vision_extract", {"image_id": image_id, "owner_id": owner})
    record = evidence()
    patch_worker_db(monkeypatch, [job, record, None])
    await worker.tick()
    assert job.status == "pending" and job.next_retry_at is not None

    monkeypatch.setattr(storage, "download_object", AsyncMock(side_effect=AppError("图片无法安全解码，请重新上传")))
    job = make_job("vision_extract", {"image_id": image_id, "owner_id": owner})
    record = evidence()
    patch_worker_db(monkeypatch, [job, record, None])
    await worker.tick()
    assert job.status == "failed" and record.status == "failed"

async def test_incomplete_embedding_batch_cannot_publish_index(monkeypatch):
    settings = Settings(DASHSCOPE_API_KEY='test',_env_file=None)
    monkeypatch.setattr('src.worker.get_settings',lambda:settings)
    gateway = MagicMock()
    gateway.embed = AsyncMock(return_value=[])
    monkeypatch.setattr('src.infra.model_gateway.get_model_gateway',lambda:gateway)
    version = SimpleNamespace(id=uuid4(),status='reviewing',index_version=None)
    chunks = [SimpleNamespace(content='换货条款',embedding=None,model_version='')]
    job = make_job('knowledge_index',{'version_id':str(version.id)})
    patch_worker_db(monkeypatch,[job,version,None],query_result=chunks)
    await worker.tick()
    assert job.status == 'pending' and version.index_version is None
    assert chunks[0].embedding is None


async def test_retrying_job_does_not_mark_outbox_failed():
    db = SimpleNamespace(scalar=AsyncMock())
    job = make_job('vision_extract', {'image_id': str(uuid4())})
    worker.schedule_retry(job, 'temporary failure')
    await worker._sync_outbox(db, job)
    db.scalar.assert_not_awaited()
    assert job.finished_at is None and job.status == 'pending'


async def test_missing_case_finalizes_outbox_even_on_early_return(monkeypatch):
    job = make_job('create_sandbox_case', {'case_id': str(uuid4())})
    event = SimpleNamespace(status='pending', processed_at=None)
    patch_worker_db(monkeypatch, [job, None, event])
    await worker.tick()
    assert job.status == 'failed' and event.status == 'failed'
    assert event.processed_at == job.finished_at


async def test_successful_knowledge_retry_clears_previous_failure(monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr('src.worker.get_settings', lambda: Settings(DASHSCOPE_API_KEY='', _env_file=None))
    version = SimpleNamespace(id=uuid4(), status='draft', index_version=None)
    chunks = [SimpleNamespace(content='政策', embedding=None, model_version='')]
    job = make_job('knowledge_index', {'version_id': str(version.id)}, attempts=1)
    job.error, job.next_retry_at = '上次索引失败', datetime.now(timezone.utc)
    patch_worker_db(monkeypatch, [job, version, None], query_result=chunks)
    await worker.tick()
    assert job.status == 'succeeded' and job.finished_at is not None
    assert job.error is None and job.next_retry_at is None
