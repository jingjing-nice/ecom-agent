"""持久化任务 Worker：工单创建、图片识别与知识索引（方案 §5/§8）。

- 多进程通过 SKIP LOCKED 领取；事务失败自动回收，next_retry_at 到期前不可再领取。
- 重试按指数退避 + 抖动，超过 max_attempts 置终态 failed；失败原因保留（删除保留策略：
  任务与外发事件记录不自动清理，供审计）。
- 工单创建走外部平台适配契约：超时/UNKNOWN 故障注入、稳定请求号核对与
  无幂等平台的人工核对分支。本适配器只创建本地演示单号，无真实退款或外部网络写操作。
"""
import asyncio
import os
import random
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import or_, select
from loguru import logger

from src.core.config import get_settings
from src.core.database import SessionFactory, dispose_engines
from src.core.exceptions import AppError
from src.models import AfterSaleCase, EvidenceObject, Job, PolicyChunk, PolicyVersion
from src.modules.aftersale.state_machine import transition_case
from src.services.knowledge import KEYWORD_ONLY
from src.services.jobs import fail_job, succeed_job, sync_outbox as _sync_outbox
from src.services.platform_adapter import get_platform_adapter, plan_after_create, plan_after_probe, stable_request_no

WORKER_ID = f"worker-{os.getpid()}"
RETRY_BASE_SECONDS = 2
RETRY_CAP_SECONDS = 300
JOB_TYPES = ["automatic_case_review", "create_sandbox_case", "vision_extract", "knowledge_index"]


def retry_delay_seconds(attempts: int, *, base: float = RETRY_BASE_SECONDS, cap: float = RETRY_CAP_SECONDS) -> float:
    """指数退避 + 抖动：封顶后附加随机量，避免多 Worker 同时重试。"""
    delay = min(base * (2 ** max(attempts - 1, 0)), cap)
    return delay * (0.5 + random.random() / 2)


def schedule_retry(job, reason: str) -> bool:
    """退避重试或终态失败。返回 True 表示已排入下一次重试。"""
    job.error, job.result, job.finished_at = reason, None, None
    if job.attempts < job.max_attempts:
        job.status = "pending"
        job.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=retry_delay_seconds(job.attempts))
        return True
    fail_job(job, reason)
    return False


async def _vision_extract(db, job) -> None:
    from src.infra.storage import download_object
    from src.services.vision import extract_image, sanitize_image

    evidence = await db.scalar(select(EvidenceObject).where(EvidenceObject.id == UUID(job.payload["image_id"]),
        EvidenceObject.tenant_id == job.tenant_id, EvidenceObject.owner_id == job.payload["owner_id"]).with_for_update())
    try:
        if evidence is None:
            raise ValueError("图片不存在")
        data = await download_object(evidence.object_key)
        data = await asyncio.to_thread(sanitize_image, data, evidence.file_name)
        result = await extract_image(data, evidence.kind, evidence.id)
        evidence.status, evidence.extracted = "extracted", result
        evidence.content_type, evidence.size_bytes = "image/jpeg", len(data)
        succeed_job(job, result)
    except AppError as exc:
        # 模型超时等可重试错误走退避；图片本身不符合限制属于不可重试。
        if not (exc.retryable and schedule_retry(job, f"识别暂时失败：{exc.message}")):
            if evidence:
                evidence.status = "failed"
            fail_job(job, "图片校验或识别失败，请检查配置、图片清晰度后重新上传")
    except Exception:
        if evidence:
            evidence.status = "failed"
        fail_job(job, "图片校验或识别失败，请检查配置、图片清晰度后重新上传")


async def _knowledge_index(db, job) -> None:
    settings = get_settings()
    version = await db.scalar(select(PolicyVersion).where(PolicyVersion.id == UUID(job.payload["version_id"]),
        PolicyVersion.tenant_id == job.tenant_id).with_for_update())
    chunks = list((await db.scalars(select(PolicyChunk).where(PolicyChunk.policy_version_id == version.id,
        PolicyChunk.tenant_id == job.tenant_id))).all()) if version else []
    if version is None:
        fail_job(job, "政策版本不存在")
        return
    if version.status not in {"draft", "reviewing"}:
        # 已发布/撤销版本不再重建索引（幂等跳过）。
        succeed_job(job, {"embedded": 0, "index_version": version.index_version, "skipped": True})
        return
    if not chunks:
        from src.retrieval.chunking import semantic_chunks
        import hashlib
        content = (job.payload or {}).get("content", "")
        if not content or hashlib.sha256(content.encode()).hexdigest() != version.checksum:
            fail_job(job, "原始文档缺失或校验失败，请重新导入")
            return
        try:
            records, chunking_meta = await semantic_chunks(content)
        except Exception:
            schedule_retry(job, "模型语义切片失败，请检查聊天模型配置或文档长度")
            return
        if not records:
            fail_job(job, "模型未产生有效切片")
            return
        chunks = [PolicyChunk(tenant_id=job.tenant_id, policy_version_id=version.id, **record) for record in records]
        for chunk in chunks:
            db.add(chunk)
        # 固化已验证切片；Embedding 重试复用这些片段，不重复请求切片模型。
        await db.flush()
        job.payload = {**job.payload, "chunking": chunking_meta}
    pending = [chunk for chunk in chunks if chunk.embedding is None or chunk.model_version != settings.EMBEDDING_MODEL]
    if not pending:
        version.index_version = version.index_version or settings.EMBEDDING_MODEL
        succeed_job(job, {"embedded": 0, "index_version": version.index_version})
    elif settings.embedding_api_key:
        try:
            from src.retrieval.rag import GatewayEmbeddings
            vectors = await GatewayEmbeddings().aembed_documents([chunk.content for chunk in pending])
            from src.services.product_search import valid_vectors
            valid_vectors(vectors, len(pending), settings.EMBEDDING_DIM)
        except Exception as exc:  # 向量服务失败：退避重试，超限后终态失败
            if not schedule_retry(job, "索引构建失败：Embedding 服务不可用，稍后重试"):
                job.error = f"索引构建失败：{exc}"
            return
        for chunk, vector in zip(pending, vectors):
            chunk.embedding, chunk.model_version = vector, settings.EMBEDDING_MODEL
        version.index_version = settings.EMBEDDING_MODEL
        succeed_job(job, {"embedded": len(pending), "index_version": settings.EMBEDDING_MODEL})
    else:
        # 未配置 Embedding：显式降级为关键词索引（检索端保留纯关键词降级路径）。
        for chunk in pending:
            chunk.model_version = KEYWORD_ONLY
        version.index_version = KEYWORD_ONLY
        succeed_job(job, {"embedded": 0, "index_version": KEYWORD_ONLY,
                          "note": "未配置 Embedding，已降级为关键词索引"})


async def _create_case(db, job) -> None:
    case = await db.scalar(select(AfterSaleCase).where(AfterSaleCase.id == UUID(job.payload["case_id"]),
        AfterSaleCase.tenant_id == job.tenant_id).with_for_update())
    if case is None:
        fail_job(job, "售后单不存在")
        return
    automatic = (getattr(case, "draft", None) or {}).get("review_mode") == "automatic"
    if automatic and case.status == "APPROVED":
        # 真正外发前重新核验政策撤销、订单状态及重复申请。
        from src.services.automatic_aftersale import automatic_review
        transition_case(case, "REVIEWING")
        await automatic_review(db, case, job.job_id, create_job=False)
        if case.status != "APPROVED":
            succeed_job(job, {"case_status": case.status, "reason": case.review_comment})
            return
    if case.status == "CREATED":
        # 重复投递：返回同一沙箱单号（不再次调用外部平台）。
        succeed_job(job, {"external_case_id": case.external_case_id})
    elif case.status == "UNKNOWN":
        # UNKNOWN 请求号核对（方案 §5）：created → CREATED；missing → 重新审批/人工核对。
        adapter = get_platform_adapter()
        probe = await adapter.query_case(request_no=stable_request_no(case))
        plan = plan_after_probe(probe.status, supports_idempotency=adapter.supports_idempotency)
        case.last_error = plan["reason"] or case.last_error
        if plan["case_status"] == "CREATED":
            transition_case(case, "CREATED")
            case.external_case_id = probe.external_case_id or case.external_case_id or f"SANDBOX-{case.id.hex}"
            succeed_job(job, {"external_case_id": case.external_case_id, "probe": "created"})
        elif automatic and probe.status == "missing" and adapter.supports_idempotency:
            transition_case(case, "APPROVED")
            schedule_retry(job, "核对未发现外部单，将重新核验政策后按同一请求号自动重试")
        elif automatic:
            # 缺少外部幂等保障时保留 UNKNOWN，停止自动写入，只继续核对。
            schedule_retry(job, "外部状态尚未确认，系统暂停创建并继续核对")
        elif plan["case_status"] == "REVIEWING":
            transition_case(case, "REVIEWING")
            succeed_job(job, {"case_status": "REVIEWING", "reason": plan["reason"]})
        else:
            schedule_retry(job, plan["reason"])
    elif case.status in {"APPROVED", "CREATING"}:
        adapter = get_platform_adapter()
        if case.status == "CREATING" and not adapter.supports_idempotency:
            # 上次创建中断且平台无幂等：不能盲目重发，转 UNKNOWN 按请求号核对。
            transition_case(case, "UNKNOWN")
            case.last_error = "平台不支持幂等请求号：创建中断后需按请求号核对"
            schedule_retry(job, case.last_error)
        else:
            if case.status == "APPROVED":
                transition_case(case, "CREATING")
                await db.flush()
            result = await adapter.create_case(request_no=stable_request_no(case), case_id=case.id.hex)
            plan = plan_after_create(result.status, supports_idempotency=adapter.supports_idempotency)
            if plan["case_status"] == "CREATED":
                transition_case(case, "CREATED")
                case.external_case_id = result.external_case_id or f"SANDBOX-{case.id.hex}"
                succeed_job(job, {"external_case_id": case.external_case_id})
            else:
                # 超时/结果未知：置 UNKNOWN，凭稳定请求号核对；无幂等平台只能人工核对。
                transition_case(case, "UNKNOWN")
                case.last_error = plan["reason"]
                schedule_retry(job, plan["reason"])
    else:
        fail_job(job, "售后单状态不允许创建")
    if job.status == "succeeded" and case.status == "CREATED":
        case.last_error = None


async def _automatic_case_review(db, job):
    from src.services.automatic_aftersale import automatic_review
    case = await db.scalar(select(AfterSaleCase).where(AfterSaleCase.id == UUID(job.payload["case_id"]),
        AfterSaleCase.tenant_id == job.tenant_id).with_for_update())
    if case is None:
        fail_job(job, "售后单不存在")
    elif case.version != job.payload["expected_version"] or case.status not in {"SUBMITTED", "REVIEWING"}:
        succeed_job(job, {"skipped": True, "case_status": case.status})
    else:
        await automatic_review(db, case, job.job_id)
        case.last_error = None
        succeed_job(job, {"case_status": case.status, "reason": case.review_comment})


async def tick() -> bool:
    async with SessionFactory.begin() as db:
        now = datetime.now(timezone.utc)
        job = await db.scalar(select(Job).where(
            Job.status == "pending", Job.job_type.in_(JOB_TYPES),
            or_(Job.next_retry_at.is_(None), Job.next_retry_at <= now),
        ).order_by(Job.created_at).with_for_update(skip_locked=True).limit(1))
        if job is None:
            return False
        job.attempts += 1
        job.locked_by, job.locked_at = WORKER_ID, now
        try:
            # 处理失败只回滚本轮业务变更，保留领取次数，防止坏任务反复杀死进程。
            async with db.begin_nested():
                if job.job_type == "automatic_case_review":
                    await _automatic_case_review(db, job)
                elif job.job_type == "vision_extract":
                    await _vision_extract(db, job)
                elif job.job_type == "knowledge_index":
                    await _knowledge_index(db, job)
                else:
                    await _create_case(db, job)
        except Exception:
            # SAVEPOINT 回滚会使 ORM 对象过期，显式刷新后更新退避信息。
            await db.refresh(job)
            schedule_retry(job, "任务执行异常，系统将按重试策略处理")
            if job.job_type in {"automatic_case_review", "create_sandbox_case"}:
                try:
                    case_id = UUID((job.payload or {}).get("case_id", ""))
                except (ValueError, TypeError, AttributeError):
                    case_id = None
                if case_id:
                    case = await db.scalar(select(AfterSaleCase).where(AfterSaleCase.id == case_id,
                        AfterSaleCase.tenant_id == job.tenant_id).with_for_update())
                    if case:
                        case.last_error = ("自动任务暂时失败，系统稍后重试" if job.status == "pending"
                            else "自动任务重试已达上限，处理已暂停")
            logger.warning("Worker 任务异常：job_id={} type={} attempt={}", job.job_id, job.job_type, job.attempts)
        await _sync_outbox(db, job)
        return True


async def main():
    try:
        while True:
            try:
                if not await tick():
                    await asyncio.sleep(1)
            except Exception:
                # 数据库连接暂时失败时也保持 Worker 存活；事务退出后锁自动释放。
                logger.warning("Worker 数据库事务失败，稍后重试")
                await asyncio.sleep(RETRY_BASE_SECONDS)
    finally:
        await dispose_engines()


if __name__ == "__main__":
    asyncio.run(main())
