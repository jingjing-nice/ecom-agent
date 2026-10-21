"""答复合成与真流式输出：create_agent + SummarizationMiddleware + 检查点（方案 §4/§9）。

覆盖：
- 数值守门只放行上下文中的独立数字 token；越界立即终止合成。
- compose_reply 逐块转发网关流；合成与摘要调用计入任务模型预算。
- Supervisor.stream() 事件顺序：progress → delta → answer；中断时不合成。
"""
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from src.agents.composer import allowed_numbers, compose_reply, reply_context, unverified_numbers
from src.agents.subagents import tool_request
from src.agents.supervisor import Supervisor
from src.agents.tools import AgentRuntime
from src.api.schemas import Chat
from src.core.config import Settings
from src.core.deps import ActorScope
from src.core.exceptions import WorkflowError

BUYER = ActorScope("buyer", "tenant", "buyer", ["a"])
CARD = {"product_id": "p1", "sku_id": "p1-41", "title": "吸顶灯", "brand": "栖家灯饰",
        "price_minor": 29900, "available_qty": 3}
SHOPPING_MESSAGE = "以下是符合条件的可售商品，价格与库存来自沙箱快照。"


@dataclass
class Hit:
    doc_id: str = "policy"
    version: str = "v1"
    content: str = "需审核"


def request(message="500 元以内的吸顶灯", **kwargs):
    return Chat(conversation_id=uuid4(), client_message_id="m", message=message, **kwargs)


def model_mode(monkeypatch, **settings):
    settings_obj = Settings(AGENT_ROUTING_MODE="model", _env_file=None, **settings)
    monkeypatch.setattr("src.agents.tools.get_settings", lambda: settings_obj)


def fake_stream(*pieces):
    async def stream(messages, tools, *, purpose):
        for piece in pieces:
            yield piece
    return stream


def runtime(payload=None):
    return AgentRuntime(MagicMock(), BUYER, payload or request(), "trace")


def shopping_answer():
    return {"agent": "shopping", "message": SHOPPING_MESSAGE, "cards": [CARD], "evidence": []}


def test_numeric_guard_only_allows_context_tokens():
    allowed = allowed_numbers(reply_context(shopping_answer()))
    assert "299.00" in allowed and "3" in allowed
    assert unverified_numbers("这款吸顶灯售价 299.00 元，现有 3 件可售。", allowed) == []
    assert unverified_numbers("现在只要 1 元。", allowed) == ["1"]
    # 结尾数字可能仍在生成中，默认不判违规
    assert unverified_numbers("只要 5", allowed) == []


async def test_compose_reply_streams_gateway_tokens(monkeypatch):
    model_mode(monkeypatch)
    monkeypatch.setattr("src.agents.tools.stream_agent_model",
                        fake_stream("这款吸顶灯售价 ", "299.00", " 元，现有 3 件可售。"))
    rt = runtime()
    chunks = [text async for text in compose_reply(rt, shopping_answer(), "compose-ok")]
    assert "".join(chunks) == "这款吸顶灯售价 299.00 元，现有 3 件可售。"
    assert rt.model_calls == 1  # 合成调用计入任务预算（§9）


async def test_nested_provider_callbacks_do_not_duplicate_reply(monkeypatch):
    from langchain_core.messages import AIMessageChunk

    class NestedStream:
        async def astream(self, *args, **kwargs):
            for text in ("换货", "请查看卡片。"):
                yield AIMessageChunk(content=text), {"langgraph_node": "model"}
                yield AIMessageChunk(content=text, response_metadata={"purpose": "agent.composer"}), {"langgraph_node": "model"}

    monkeypatch.setattr('src.agents.composer.build_composer', lambda rt: NestedStream())
    chunks = [text async for text in compose_reply(runtime(), shopping_answer(), 'nested')]
    assert ''.join(chunks) == '换货请查看卡片。'


async def test_compose_reply_aborts_on_unverified_number(monkeypatch):
    model_mode(monkeypatch)
    monkeypatch.setattr("src.agents.tools.stream_agent_model", fake_stream("现在只要 ", "1", " 元。"))
    rt = runtime()
    seen = []
    with pytest.raises(WorkflowError, match="未核验数值"):
        async for text in compose_reply(rt, shopping_answer(), "compose-bad"):
            seen.append(text)
    # 不完整或未核验的数值不能先流出到用户界面
    assert "".join(seen) == "现在只要 "


async def test_summarizer_calls_do_not_leak_into_reply(monkeypatch):
    """摘要中间件的模型输出与注入消息不得混入回复（同一线程触发压缩）。"""
    model_mode(monkeypatch, COMPOSER_SUMMARY_TRIGGER_TOKENS=1, COMPOSER_KEEP_MESSAGES=2)
    purposes = []

    async def stream(messages, tools, *, purpose):
        purposes.append(purpose)
        if purpose == "agent.summarizer":
            yield "【摘要】历史压缩内容"
        else:
            for piece in ("自然", "回复"):
                yield piece

    monkeypatch.setattr("src.agents.tools.stream_agent_model", stream)
    rt = runtime()
    thread = "compose-sum"
    for _ in range(2):
        chunks = [text async for text in compose_reply(rt, shopping_answer(), thread)]
    assert "".join(chunks) == "自然回复"
    assert all("摘要" not in text for text in chunks)
    assert "agent.summarizer" in purposes  # 触发过摘要压缩


async def test_supervisor_stream_emits_progress_then_tokens_then_answer(monkeypatch):
    model_mode(monkeypatch)
    monkeypatch.setattr("src.agents.tools.call_agent_model", AsyncMock(side_effect=[
        AIMessage(content="", tool_calls=[tool_request("shopping_agent")]),
        AIMessage(content="", tool_calls=[tool_request("search_products", {"category": "吸顶灯"})]),
        AIMessage(content="模型自由文本"), AIMessage(content="完成"),
    ]))
    monkeypatch.setattr("src.agents.tools.search_catalog", AsyncMock(return_value=[CARD]))
    monkeypatch.setattr("src.agents.tools.stream_agent_model",
                        fake_stream("为您找到 ", "299.00", " 元的吸顶灯，可查看下方卡片。"))

    events = [item async for item in Supervisor(MagicMock(), BUYER, "trace").stream(request())]

    kinds = [item["event"] for item in events]
    assert kinds[-1] == "answer" and "delta" in kinds
    progress = [item["data"] for item in events if item["event"] == "progress"]
    assert [step["tool"] for step in progress] == ["search_products"]
    assert kinds.index("delta") > max(index for index, kind in enumerate(kinds) if kind == "progress")
    answer = events[-1]["data"]
    assert answer["reply"] == "为您找到 299.00 元的吸顶灯，可查看下方卡片。"
    assert answer["cards"][0]["price_minor"] == 29900
    assert answer["message"] == SHOPPING_MESSAGE
    # 正文合成不计入卡片与数值口径，但模型调用预算包含合成调用
    assert answer["model_calls"] == 5


async def test_stream_falls_back_when_composer_number_unverified(monkeypatch):
    model_mode(monkeypatch)
    monkeypatch.setattr("src.agents.tools.call_agent_model", AsyncMock(side_effect=[
        AIMessage(content="", tool_calls=[tool_request("shopping_agent")]),
        AIMessage(content="", tool_calls=[tool_request("search_products")]),
        AIMessage(content="完成"), AIMessage(content="完成"),
    ]))
    monkeypatch.setattr("src.agents.tools.search_catalog", AsyncMock(return_value=[CARD]))
    monkeypatch.setattr("src.agents.tools.stream_agent_model", fake_stream("现在只要 ", "1", " 元。"))
    events = [item async for item in Supervisor(MagicMock(), BUYER, "trace").stream(request())]
    answer = events[-1]["data"]
    assert "reply" not in answer
    assert answer["message"] == SHOPPING_MESSAGE
    assert answer["cards"][0]["price_minor"] == 29900


async def test_stream_skips_composer_when_budget_exhausted(monkeypatch):
    model_mode(monkeypatch, TASK_MAX_MODEL_CALLS=2)
    stream_mock = AsyncMock()
    monkeypatch.setattr("src.agents.tools.call_agent_model", AsyncMock(side_effect=[
        AIMessage(content="", tool_calls=[tool_request("shopping_agent")]),
        AIMessage(content="", tool_calls=[tool_request("search_products")]),
    ]))
    monkeypatch.setattr("src.agents.tools.search_catalog", AsyncMock(return_value=[CARD]))
    monkeypatch.setattr("src.agents.tools.stream_agent_model", stream_mock)
    events = [item async for item in Supervisor(MagicMock(), BUYER, "trace").stream(request())]
    answer = events[-1]["data"]
    assert "reply" not in answer  # 预算耗尽时合成不发起调用
    assert answer["model_calls"] == 2
    assert stream_mock.call_count == 0


async def test_stream_pauses_for_human_approval_without_composer(monkeypatch):
    monkeypatch.setattr("src.agents.tools.get_settings", lambda: Settings(
        AGENT_ROUTING_MODE="rules", AGENT_HUMAN_APPROVAL=True, _env_file=None))
    monkeypatch.setattr("src.agents.tools.owned_item", AsyncMock(return_value=(
        SimpleNamespace(product_id="p1", brand_snapshot="brand", category_snapshot="吸顶灯",
                        quantity=1, paid_amount_minor=29900),
        SimpleNamespace(shop_id="a"))))
    monkeypatch.setattr("src.retrieval.rag.retrieve_policy", AsyncMock(return_value={"evidence":[{"doc_id":"policy","version":"v1","content":"需审核"}],"conflicts":[],"answer":""}))
    events = [item async for item in Supervisor(MagicMock(), BUYER, "trace").stream(
        request("换大一号", order_item_id=uuid4(), target_size="42"))]
    answer = events[-1]["data"]
    assert answer["requires_human_approval"] and answer["thread_id"]
    assert all(item["event"] != "delta" for item in events)

@pytest.mark.parametrize("pieces", [
    ("售价 ", "299", ".", "00", " 元。"),
    ("售价 ", "2", "99.0", "0"),
])
async def test_split_decimal_is_buffered_until_verified(monkeypatch, pieces):
    model_mode(monkeypatch)
    monkeypatch.setattr("src.agents.tools.stream_agent_model", fake_stream(*pieces))
    chunks = [text async for text in compose_reply(runtime(), shopping_answer(), str(uuid4()))]
    assert "".join(chunks) == "".join(pieces)
    assert chunks[0] == "售价 "
    assert chunks[1].startswith("299.00")


async def test_invalid_trailing_number_never_leaves_composer(monkeypatch):
    model_mode(monkeypatch)
    monkeypatch.setattr("src.agents.tools.stream_agent_model", fake_stream("只要 ", "999"))
    chunks = []
    with pytest.raises(WorkflowError, match="未核验数值"):
        async for text in compose_reply(runtime(), shopping_answer(), str(uuid4())):
            chunks.append(text)
    assert "".join(chunks) == "只要 "


async def test_stream_close_waits_for_graph_cleanup(monkeypatch):
    import asyncio

    started, cleaned = asyncio.Event(), asyncio.Event()

    def astream(*args, **kwargs):
        async def events():
            started.set()
            try:
                yield ((), "custom", {"type": "progress", "index": 0})
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()
        return events()

    rt = SimpleNamespace(settings=SimpleNamespace(TASK_DEADLINE_SECONDS=1),
                         trace=[{"tool": "started"}])
    supervisor = Supervisor(MagicMock(), BUYER, "trace")
    monkeypatch.setattr(supervisor, "_build", lambda *args: (SimpleNamespace(astream=astream), rt))
    stream = supervisor.stream(request())
    assert (await anext(stream))["event"] == "progress"
    assert started.is_set()
    await stream.aclose()
    assert cleaned.is_set()


async def test_composer_shares_workflow_deadline(monkeypatch):
    import asyncio

    cleaned = asyncio.Event()

    async def slow_composer(*args):
        try:
            await asyncio.Event().wait()
            yield "不会执行"
        finally:
            cleaned.set()

    settings = SimpleNamespace(TASK_DEADLINE_SECONDS=0.02, AGENT_REPLY_COMPOSER=True,
                               TASK_MAX_MODEL_CALLS=10)
    rt = SimpleNamespace(settings=settings, trace=[], use_model=True, model_calls=0)
    def astream(*args, **kwargs):
        async def events():
            yield ((), "values", {"answer": shopping_answer()})
        return events()
    graph = SimpleNamespace(astream=astream)
    supervisor = Supervisor(MagicMock(), BUYER, "trace")
    monkeypatch.setattr(supervisor, "_build", lambda *args: (graph, rt))
    monkeypatch.setattr("src.agents.supervisor.compose_reply", slow_composer)
    async with asyncio.timeout(1):
        events = [event async for event in supervisor.stream(request())]
    assert cleaned.is_set()
    assert events[-1]["event"] == "answer"
    assert events[-1]["data"]["message"] == SHOPPING_MESSAGE
    assert "reply" not in events[-1]["data"]
