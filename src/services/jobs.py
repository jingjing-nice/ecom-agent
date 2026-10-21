"""任务入队与生命周期；只允许安全重建的识别和索引任务通用重放。"""
from datetime import datetime, timezone
from uuid import UUID, uuid4
from sqlalchemy import select
from src.core.exceptions import Conflict, NotFound, PermissionDenied
from src.models import AuditLog, EvidenceObject, Job, OutboxEvent, PolicyVersion


TERMINAL_JOB_STATUSES = {"succeeded", "failed"}


def enqueue_job(db, *, tenant_id, job_type, payload, job_id=None):
    """只登记任务，与调用方业务变更同事务提交，不在服务内 commit。"""
    job = Job(tenant_id=tenant_id, job_id=job_id or uuid4().hex,
              job_type=job_type, payload=payload, status="pending")
    db.add(job)
    return job


def succeed_job(job, result):
    job.status, job.result = "succeeded", result
    job.error = job.next_retry_at = None
    job.finished_at = datetime.now(timezone.utc)


def fail_job(job, reason):
    job.status, job.error = "failed", reason
    job.result = job.next_retry_at = None
    job.finished_at = datetime.now(timezone.utc)


async def sync_outbox(db, job):
    """只有终态才能结束 outbox，排队重试不能被记录成失败。"""
    if job.status not in TERMINAL_JOB_STATUSES:
        return
    event = await db.scalar(select(OutboxEvent).where(
        OutboxEvent.tenant_id == job.tenant_id, OutboxEvent.event_id == job.job_id))
    if event:
        event.status = "processed" if job.status == "succeeded" else "failed"
        event.processed_at = job.finished_at


async def retry_job(db, actor, job_id, trace_id):
    job = await db.scalar(select(Job).where(Job.job_id == job_id, Job.tenant_id == actor.tenant_id).with_for_update())
    if job is None or (job.payload or {}).get('owner_id') != actor.user_id:
        raise NotFound('任务不存在')
    expected_role = {'vision_extract':'buyer', 'knowledge_index':'knowledge_admin'}.get(job.job_type)
    if expected_role != actor.role:
        raise PermissionDenied('此任务不能通过通用入口重放；工单请按状态核对或重新审核')
    if job.status != 'failed':
        raise Conflict('仅失败任务可以重放')
    if job.job_type == 'vision_extract':
        resource = await db.scalar(select(EvidenceObject).where(EvidenceObject.id == UUID(job.payload['image_id']),
            EvidenceObject.tenant_id == actor.tenant_id, EvidenceObject.owner_id == actor.user_id).with_for_update())
        if resource is None:
            raise NotFound('图片已不存在，不能重放')
        if resource.status != 'failed':
            raise Conflict('图片状态已更新，不能重放')
        resource.status = 'extracting'
    else:
        resource = await db.scalar(select(PolicyVersion).where(PolicyVersion.id == UUID(job.payload['version_id']),
            PolicyVersion.tenant_id == actor.tenant_id).with_for_update())
        if resource is None:
            raise NotFound('政策版本已不存在')
        if resource.status not in {'draft','reviewing'}:
            raise Conflict('已发布或已撤销的版本不能重建')
    job.status, job.attempts = 'pending', 0
    job.next_retry_at = job.locked_by = job.locked_at = job.finished_at = None
    job.error, job.result = None, None
    db.add(AuditLog(tenant_id=actor.tenant_id,actor_id=actor.user_id,action='job.retried',
        resource_type='job',resource_id=job.job_id,trace_id=trace_id,
        detail={'job_type':job.job_type},created_at=datetime.now(timezone.utc)))
    return job
