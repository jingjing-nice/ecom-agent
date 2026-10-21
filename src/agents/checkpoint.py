"""LangGraph 层：线程与检查点，负责状态持久化与恢复。

职责边界（LangGraph 侧）：
- 图编译时挂载 checkpointer；每次运行以 thread_id 标识一条可恢复的线程。
- 人工审批暂停后，凭同一 thread_id 通过 Command(resume=...) 从断点继续。
- 后端选择：CHECKPOINT_BACKEND=redis 时使用 AsyncRedisSaver（跨进程恢复，依赖
  RediSearch/RedisJSON，compose 使用 redis-stack 镜像）；postgres 时使用官方
  AsyncPostgresSaver（复用业务库实例，首次装配自动建表）；auto 下 Redis 不可用
  回退进程内 InMemorySaver；memory 始终为进程内。切换后端不改动图与节点。
"""
from __future__ import annotations

from uuid import uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from loguru import logger

from src.core.config import Settings, get_settings
from src.core.exceptions import WorkflowError

# 进程级注册位：保证同一 thread_id 的中断状态可被 resume 命中。
_saver: BaseCheckpointSaver = InMemorySaver()
_backend = "memory"
# 持久化后端的异步上下文随进程存活，关闭时统一释放。
_redis_context = None
_pg_context = None


def build_checkpointer() -> BaseCheckpointSaver:
    """返回当前检查点后端；由 configure_checkpointer() 在应用启动时装配。"""
    return _saver


def checkpoint_backend() -> str:
    """当前后端名称（memory / redis / postgres），供健康检查与日志使用。"""
    return _backend


def checkpoint_ttl(settings: Settings) -> dict | None:
    """Redis 检查点保留策略：正数分钟启用 TTL，0 表示不过期。Postgres 不支持此参数。"""
    minutes = settings.CHECKPOINT_TTL_MINUTES
    if minutes and minutes > 0:
        return {"default_ttl": minutes, "refresh_on_read": True}
    return None


async def configure_checkpointer(settings: Settings | None = None) -> str:
    """按配置装配检查点后端。

    - redis：强制持久化，连接或索引初始化失败直接报错（启动即失败，避免静默丢失恢复能力）。
    - postgres：官方 AsyncPostgresSaver 复用业务库实例，连接或建表失败直接报错。
    - auto：优先 Redis，不可用则回退 InMemorySaver 并提示人工审批仅限当前进程。
    - memory：仅进程内（默认，测试与本地最小依赖运行）。
    """
    global _saver, _backend, _redis_context, _pg_context
    settings = settings or get_settings()
    if settings.CHECKPOINT_BACKEND == _backend:
        return _backend
    await aclose_checkpointer()
    if settings.CHECKPOINT_BACKEND == "memory":
        return _backend

    if settings.CHECKPOINT_BACKEND == "postgres":
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            context = AsyncPostgresSaver.from_conn_string(settings.checkpoint_pg_url)
            saver = await context.__aenter__()
            try:
                await saver.setup()
            except BaseException:
                await context.__aexit__(None, None, None)
                raise
        except Exception as exc:
            raise WorkflowError(f"Postgres 检查点后端不可用：{exc}") from exc
        _pg_context, _saver, _backend = context, saver, "postgres"
        logger.info(f"检查点后端已装配：postgres（{settings.redacted_checkpoint_pg_url}）")
        return _backend

    try:
        from langgraph.checkpoint.redis.aio import AsyncRedisSaver

        context = AsyncRedisSaver.from_conn_string(settings.redis_url, ttl=checkpoint_ttl(settings))
        saver = await context.__aenter__()
        try:
            await saver.asetup()
        except BaseException:
            await context.__aexit__(None, None, None)
            raise
    except Exception as exc:
        if settings.CHECKPOINT_BACKEND == "redis":
            raise WorkflowError(f"Redis 检查点后端不可用：{exc}") from exc
        logger.warning(f"Redis 检查点不可用，回退 InMemorySaver（人工审批仅限当前进程）：{exc}")
        return _backend

    _redis_context, _saver, _backend = context, saver, "redis"
    logger.info(f"检查点后端已装配：redis（{settings.redact_redis_url}）")
    return _backend


async def aclose_checkpointer() -> None:
    """释放持久化检查点连接（应用关闭时调用）。"""
    global _redis_context, _pg_context, _saver, _backend
    for context in (_redis_context, _pg_context):
        if context is not None:
            await context.__aexit__(None, None, None)
    _redis_context = _pg_context = None
    _saver, _backend = InMemorySaver(), "memory"


def new_thread_id() -> str:
    """为一次任务生成唯一 thread_id，避免不同请求复用同一恢复线程。"""
    return uuid4().hex


def thread_config(thread_id: str) -> dict:
    """构造带 thread_id 的运行配置，供 ainvoke / astream 使用。"""
    return {"configurable": {"thread_id": thread_id}}