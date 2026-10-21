"""检查点保留策略与跨进程演练装置自检（方案 §8）。

- checkpoint_ttl：保留策略组装（分钟换算与不过期分支）
- configure_checkpointer：TTL 透传 AsyncRedisSaver；auto 后端在 Redis 不可用时回退内存
- 演练图：interrupt 审批门 + Command(resume) 恢复（内存后端验证图语义；
  真实跨进程恢复由 scripts/checkpoint_drill.py --run 在 Redis Stack 环境执行）
- validate_drill_pair：跨进程核对项的负例覆盖
"""
import pytest

from scripts.checkpoint_drill import build_drill_graph, validate_drill_pair
from src.agents.checkpoint import (
    aclose_checkpointer,
    checkpoint_ttl,
    configure_checkpointer,
)
from src.core.config import Settings


def test_checkpoint_ttl_policy_minutes_and_never_expire():
    assert checkpoint_ttl(Settings(CHECKPOINT_TTL_MINUTES=43200, _env_file=None)) == {
        "default_ttl": 43200, "refresh_on_read": True}
    assert checkpoint_ttl(Settings(CHECKPOINT_TTL_MINUTES=1, _env_file=None)) == {
        "default_ttl": 1, "refresh_on_read": True}
    assert checkpoint_ttl(Settings(CHECKPOINT_TTL_MINUTES=0, _env_file=None)) is None


async def test_configure_checkpointer_passes_ttl_and_falls_back(monkeypatch):
    await aclose_checkpointer()
    captured: dict = {}

    class FakeSaver:
        async def asetup(self):
            captured["setup"] = True

    class FakeContext:
        async def __aenter__(self):
            return FakeSaver()

        async def __aexit__(self, *exc):
            return False

    def fake_from_conn_string(redis_url=None, *, ttl=None, **kwargs):
        captured["url"], captured["ttl"] = redis_url, ttl
        return FakeContext()

    monkeypatch.setattr(
        "langgraph.checkpoint.redis.aio.AsyncRedisSaver.from_conn_string",
        fake_from_conn_string)
    backend = await configure_checkpointer(
        Settings(CHECKPOINT_BACKEND="redis", CHECKPOINT_TTL_MINUTES=60, _env_file=None))
    assert backend == "redis" and captured["setup"] is True
    assert captured["ttl"] == {"default_ttl": 60, "refresh_on_read": True}
    await aclose_checkpointer()

    def broken_from_conn_string(*args, **kwargs):
        raise ConnectionError("redis 不可达")

    monkeypatch.setattr(
        "langgraph.checkpoint.redis.aio.AsyncRedisSaver.from_conn_string",
        broken_from_conn_string)
    backend = await configure_checkpointer(Settings(CHECKPOINT_BACKEND="auto", _env_file=None))
    assert backend == "memory"  # auto 回退进程内，不静默丢失提示由日志承担


async def test_drill_graph_interrupts_then_resumes_preserving_summary():
    from langchain_core.messages import HumanMessage
    from langgraph.types import Command

    from src.agents.checkpoint import thread_config

    await aclose_checkpointer()
    assert await configure_checkpointer(Settings(CHECKPOINT_BACKEND="memory", _env_file=None)) == "memory"
    graph = build_drill_graph()
    config = thread_config("drill-test-thread")
    state = await graph.ainvoke({"messages": [HumanMessage(content="咨询尺寸")], "summary": ""}, config)
    assert state.get("__interrupt__")
    snapshot = await graph.aget_state(config)
    assert snapshot.values.get("summary")
    assert len(snapshot.values.get("messages", [])) == 1

    resumed = await graph.ainvoke(Command(resume={"decision": "approve", "by": "tester"}), config)
    assert resumed["approval"]["decision"] == "approve"
    assert resumed["summary"] == snapshot.values["summary"]
    await aclose_checkpointer()


def _drill_pair(**overrides):
    pause = {
        "stage": "pause", "backend": "redis", "thread_id": "t-1", "interrupted": True,
        "summary": "摘要", "messages": [{"type": "human", "content": "第 1 轮"}],
    }
    resume = {
        "stage": "resume", "backend": "redis", "thread_id": "t-1", "found_checkpoint": True,
        "restored_summary": "摘要", "restored_messages": [{"type": "human", "content": "第 1 轮"}],
        "summary": "摘要", "messages": [{"type": "human", "content": "第 1 轮"}],
        "approval": {"decision": "approve", "by": "checkpoint-drill"},
    }
    pause.update(overrides.get("pause", {}))
    resume.update(overrides.get("resume", {}))
    return pause, resume


def test_validate_drill_pair_accepts_consistent_pair():
    pause, resume = _drill_pair()
    assert validate_drill_pair(pause, resume) == []


@pytest.mark.parametrize("overrides,fragment", [
    ({"pause": {"interrupted": False}}, "未在人工审批门暂停"),
    ({"resume": {"found_checkpoint": False}}, "未找到持久化检查点"),
    ({"resume": {"restored_summary": "丢失"}}, "摘要字段不一致"),
    ({"resume": {"restored_messages": []}}, "消息序列不一致"),
    ({"resume": {"approval": {"decision": "reject"}}}, "审批决策未写回"),
    ({"pause": {"backend": "memory"}}, "redis 检查点后端"),
])
def test_validate_drill_pair_reports_problems(overrides, fragment):
    pause, resume = _drill_pair(**overrides)
    problems = validate_drill_pair(pause, resume)
    assert any(fragment in item for item in problems)