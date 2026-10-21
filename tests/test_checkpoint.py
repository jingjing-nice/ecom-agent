"""检查点后端装配：memory / auto 回退 / redis / postgres 接线（不依赖真实 Redis/PG）。"""
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from src.agents import checkpoint
from src.core.config import Settings
from src.core.exceptions import WorkflowError


async def test_memory_backend_keeps_in_process_saver():
    await checkpoint.aclose_checkpointer()
    backend = await checkpoint.configure_checkpointer(Settings(CHECKPOINT_BACKEND="memory", _env_file=None))
    assert backend == "memory"
    assert checkpoint.checkpoint_backend() == "memory"
    assert isinstance(checkpoint.build_checkpointer(), InMemorySaver)


async def test_auto_falls_back_when_redis_unavailable(monkeypatch):
    await checkpoint.aclose_checkpointer()
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

    def broken(url, **kwargs):
        raise RuntimeError("redis down")

    monkeypatch.setattr(AsyncRedisSaver, "from_conn_string", broken)
    backend = await checkpoint.configure_checkpointer(Settings(CHECKPOINT_BACKEND="auto", _env_file=None))
    assert backend == "memory"
    assert isinstance(checkpoint.build_checkpointer(), InMemorySaver)


async def test_forced_redis_failure_fails_fast(monkeypatch):
    await checkpoint.aclose_checkpointer()
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

    def broken(url, **kwargs):
        raise RuntimeError("redis down")

    monkeypatch.setattr(AsyncRedisSaver, "from_conn_string", broken)
    with pytest.raises(WorkflowError, match="Redis 检查点后端不可用"):
        await checkpoint.configure_checkpointer(Settings(CHECKPOINT_BACKEND="redis", _env_file=None))


async def test_redis_backend_is_wired_and_released(monkeypatch):
    await checkpoint.aclose_checkpointer()
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

    class FakeSaver:
        ready = False

        async def asetup(self):
            self.ready = True

    saver = FakeSaver()

    class FakeContext:
        exited = False

        async def __aenter__(self):
            return saver

        async def __aexit__(self, *exc):
            self.exited = True
            return False

    context = FakeContext()
    monkeypatch.setattr(AsyncRedisSaver, "from_conn_string", lambda url, **kwargs: context)

    backend = await checkpoint.configure_checkpointer(Settings(CHECKPOINT_BACKEND="redis", _env_file=None))
    assert backend == "redis" and saver.ready
    assert checkpoint.build_checkpointer() is saver

    await checkpoint.aclose_checkpointer()
    assert context.exited and isinstance(checkpoint.build_checkpointer(), InMemorySaver)


async def test_forced_postgres_failure_fails_fast(monkeypatch):
    await checkpoint.aclose_checkpointer()
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    def broken(conn_string, **kwargs):
        raise RuntimeError("pg down")

    monkeypatch.setattr(AsyncPostgresSaver, "from_conn_string", broken)
    with pytest.raises(WorkflowError, match="Postgres 检查点后端不可用"):
        await checkpoint.configure_checkpointer(Settings(CHECKPOINT_BACKEND="postgres", _env_file=None))


async def test_postgres_backend_is_wired_and_released(monkeypatch):
    await checkpoint.aclose_checkpointer()
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    class FakeSaver:
        ready = False

        async def setup(self):
            self.ready = True

    saver = FakeSaver()

    class FakeContext:
        exited = False

        async def __aenter__(self):
            return saver

        async def __aexit__(self, *exc):
            self.exited = True
            return False

    context = FakeContext()
    seen = {}

    def factory(conn_string, *, pipeline=False, serde=None):
        seen["conn_string"] = conn_string
        return context

    monkeypatch.setattr(AsyncPostgresSaver, "from_conn_string", factory)
    settings = Settings(CHECKPOINT_BACKEND="postgres", _env_file=None, DB_PASSWORD="p@ss")
    backend = await checkpoint.configure_checkpointer(settings)
    assert backend == "postgres" and saver.ready
    assert checkpoint.build_checkpointer() is saver
    # 连接串复用业务库凭据；替身签名与官方 API 一致，不接受 Redis TTL
    assert seen["conn_string"] == settings.checkpoint_pg_url
    assert "p%40ss" in seen["conn_string"]

    await checkpoint.aclose_checkpointer()
    assert context.exited and isinstance(checkpoint.build_checkpointer(), InMemorySaver)