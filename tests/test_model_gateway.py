"""模型网关：LangChain 传输层的计量与工具调用契约（方案 §9）。

网关仍是唯一模型出口——LangChain 负责模型调用与结构化解析，计量、熔断与错误收敛在此。
chat() 直接进出 LangChain 消息；structured() 走官方 with_structured_output 解析链路。
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from pydantic import BaseModel, Field

from src.core.config import Settings
from src.core.exceptions import WorkflowError
from src.infra.model_gateway import ModelGateway


class StubModel:
    """替身 ChatModel：记录入参并返回预置 AIMessage。"""

    def __init__(self, response):
        self.response = response
        self.bound_tools = None
        self.received = None

    def bind_tools(self, tools):
        self.bound_tools = list(tools)
        return self

    async def ainvoke(self, messages):
        self.received = messages
        return self.response


class WrapperStub:
    """替身 Runnable 绑定：structured() 用 with_structured_output(schema) 构建解析链。"""

    def __init__(self, inner=None, parser=None):
        self.inner = inner
        self.parser = parser

    def with_structured_output(self, schema, **kwargs):
        return WrapperStub(self.inner, parser=schema)

    async def ainvoke(self, messages):
        content = await self.inner.ainvoke(messages) if self.inner is not None else self.inner
        text = content if isinstance(content, str) else getattr(content, "content", "")
        if self.parser is None:
            return {"parsed": None, "raw": AIMessage(content=text), "parsing_error": None}
        try:
            parsed = self.parser.model_validate_json(text if isinstance(text, str) else str(text))
        except Exception as exc:
            return {"parsed": None, "raw": AIMessage(content=text), "parsing_error": exc}
        return {"parsed": parsed, "raw": AIMessage(content=text), "parsing_error": None}


def build_gateway(monkeypatch, key="test-key"):
    monkeypatch.setattr("src.infra.model_gateway.get_settings",
                        lambda: Settings(DEEPSEEK_API_KEY=key, _env_file=None))
    return ModelGateway()


async def test_chat_returns_langchain_message_with_metering(monkeypatch):
    gateway = build_gateway(monkeypatch)
    response = AIMessage(
        content="已为你筛选",
        tool_calls=[{"name": "search_products", "args": {"category": "吸顶灯"}, "id": "c1", "type": "tool_call"}],
        usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        response_metadata={"finish_reason": "tool_calls"},
    )
    stub = StubModel(response)
    monkeypatch.setattr(gateway, "_chat_model", lambda *a, **k: stub)

    message = await gateway.chat([HumanMessage(content="500 元以内的吸顶灯")], tools=[])

    assert isinstance(message, AIMessage)
    assert message.tool_calls[0]["name"] == "search_products"
    assert message.tool_calls[0]["args"] == {"category": "吸顶灯"}
    assert message.usage_metadata["input_tokens"] == 11
    assert message.response_metadata["gateway"]["model"] == "deepseek-chat"
    assert message.response_metadata["gateway"]["cost_estimate_yuan"] > 0


async def test_openai_style_dict_messages_are_accepted(monkeypatch):
    """NL2SQL 等调用方仍可传 OpenAI 风格字典。"""
    gateway = build_gateway(monkeypatch)
    stub = StubModel(AIMessage(content="ok"))
    monkeypatch.setattr(gateway, "_chat_model", lambda *a, **k: stub)

    await gateway.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}])
    assert [type(m).__name__ for m in stub.received] == ["SystemMessage", "HumanMessage"]


async def test_invalid_tool_arguments_are_rejected(monkeypatch):
    gateway = build_gateway(monkeypatch)
    response = AIMessage(content="")
    response.invalid_tool_calls = [{"name": "search_products", "args": "{bad", "id": "c1", "error": "bad json"}]
    monkeypatch.setattr(gateway, "_chat_model", lambda *a, **k: StubModel(response))

    with pytest.raises(WorkflowError, match="无效的工具参数"):
        await gateway.chat([HumanMessage(content="hi")])


async def test_duplicate_tool_call_ids_are_rejected(monkeypatch):
    gateway = build_gateway(monkeypatch)
    response = AIMessage(content="", tool_calls=[
        {"name": "search_products", "args": {}, "id": "c1", "type": "tool_call"},
        {"name": "graph_lookup", "args": {}, "id": "c1", "type": "tool_call"},
    ])
    monkeypatch.setattr(gateway, "_chat_model", lambda *a, **k: StubModel(response))

    with pytest.raises(WorkflowError, match="重复的工具调用 ID"):
        await gateway.chat([HumanMessage(content="hi")])


async def test_missing_api_key_never_reaches_provider(monkeypatch):
    gateway = build_gateway(monkeypatch, key="")
    with pytest.raises(WorkflowError, match="未配置主模型"):
        await gateway.chat([HumanMessage(content="hi")])


async def test_circuit_opens_after_repeated_failures(monkeypatch):
    gateway = build_gateway(monkeypatch)
    failing = StubModel(AIMessage(content=""))
    failing.ainvoke = AsyncMock(side_effect=RuntimeError("provider down"))
    monkeypatch.setattr(gateway, "_chat_model", lambda *a, **k: failing)

    for _ in range(5):
        with pytest.raises(WorkflowError, match="模型调用失败"):
            await gateway.chat([HumanMessage(content="hi")])
    # 熔断打开后不再打到 provider
    with pytest.raises(WorkflowError, match="熔断中"):
        await gateway.chat([HumanMessage(content="hi")])


class Sample(BaseModel):
    answer: str


class Strict(BaseModel):
    answer: str = Field(min_length=3)


async def test_structured_parses_schema_and_attaches_parsed_message(monkeypatch):
    gateway = build_gateway(monkeypatch)
    monkeypatch.setattr(gateway, "chat_runnable", lambda **k: WrapperStub(StubModel(AIMessage(content='{"answer": "ok"}'))))

    message = await gateway.structured([HumanMessage(content="hi")], Sample, purpose="test")

    assert isinstance(message, AIMessage)
    assert message.response_metadata["parsed"].answer == "ok"
    assert message.response_metadata["gateway"]["model"] == "deepseek-chat"


async def test_structured_parse_failure_raises(monkeypatch):
    gateway = build_gateway(monkeypatch)
    monkeypatch.setattr(gateway, "chat_runnable", lambda **k: WrapperStub(StubModel(AIMessage(content='{"answer": "x"}'))))
    with pytest.raises(WorkflowError, match="schema"):
        await gateway.structured([HumanMessage(content="hi")], Strict, purpose="test")


async def test_structured_clean_callback_tolerates_non_core_fields(monkeypatch):
    def drop_short_answer(sample):
        if len(sample.answer) < 2:
            sample.answer = ""
        return sample

    gateway = build_gateway(monkeypatch)
    monkeypatch.setattr(gateway, "chat_runnable", lambda **k: WrapperStub(StubModel(AIMessage(content='{"answer": "y"}'))))
    message = await gateway.structured([HumanMessage(content="hi")], Sample, purpose="test", clean=drop_short_answer)
    assert message.response_metadata["parsed"].answer == ""


class StreamingStub:
    """替身 ChatModel：逐块产出文本的流式接口。"""

    def __init__(self, *chunks):
        self.chunks = chunks

    def bind_tools(self, tools):
        return self

    async def astream(self, messages):
        for chunk in self.chunks:
            yield chunk


async def test_astream_chat_yields_token_chunks_in_order(monkeypatch):
    gateway = build_gateway(monkeypatch)
    stub = StreamingStub(
        AIMessageChunk(content="你好", usage_metadata={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}),
        AIMessageChunk(content="，世界"),
        AIMessageChunk(content=""),  # 尾块无文本不应产出
    )
    monkeypatch.setattr(gateway, "_chat_model", lambda *a, **k: stub)

    pieces = [text async for text in gateway.astream_chat([HumanMessage(content="hi")], purpose="test")]
    assert pieces == ["你好", "，世界"]


async def test_astream_failure_counts_toward_circuit(monkeypatch):
    gateway = build_gateway(monkeypatch)

    class FailingStub:
        def bind_tools(self, tools):
            return self

        async def astream(self, messages):
            raise RuntimeError("provider down")
            yield  # pragma: no cover - 保持异步生成器形态

    monkeypatch.setattr(gateway, "_chat_model", lambda *a, **k: FailingStub())
    for _ in range(5):
        with pytest.raises(WorkflowError, match="模型调用失败"):
            [text async for text in gateway.astream_chat([HumanMessage(content="hi")])]
    with pytest.raises(WorkflowError, match="熔断中"):
        [text async for text in gateway.astream_chat([HumanMessage(content="hi")])]

async def test_embedding_batches_and_validates_dimension(monkeypatch):
    monkeypatch.setattr('src.infra.model_gateway.get_settings', lambda: Settings(DASHSCOPE_API_KEY='test', EMBEDDING_DIM=2, _env_file=None))
    gateway = ModelGateway()
    # 官方 OpenAIEmbeddings 负责分批与请求；网关负责返回前后的数量与维度校验。
    fake = SimpleNamespace(aembed_documents=AsyncMock(return_value=[[1.0, 0.0], [0.0, 1.0]]))
    monkeypatch.setattr(gateway, "_embedding_model", lambda: fake)
    vectors = await gateway.embed(['第一条', '第二条'])
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    fake.aembed_documents.assert_awaited_once_with(['第一条', '第二条'])

    broken = SimpleNamespace(aembed_documents=AsyncMock(return_value=[[1.0, 0.0, 5.0], [0.0, 1.0]]))
    monkeypatch.setattr(gateway, "_embedding_model", lambda: broken)
    with pytest.raises(WorkflowError, match="维度或数值"):
        await gateway.embed(['a', 'b'])

    missing = SimpleNamespace(aembed_documents=AsyncMock(return_value=[[1.0, 0.0]]))
    monkeypatch.setattr(gateway, "_embedding_model", lambda: missing)
    with pytest.raises(WorkflowError, match="数量不完整"):
        await gateway.embed(['a', 'b'])


def test_independent_embedding_settings_and_dimension_alias():
    settings = Settings(EMBEDDING_API_KEY='embedding-only',
        EMBEDDING_BASE_URL='https://embedding.example/v1', EMBEDDING_DIMENSION=1024,
        DASHSCOPE_API_KEY='vision-only', _env_file=None)
    assert settings.embedding_api_key == 'embedding-only'
    assert settings.embedding_base_url == 'https://embedding.example/v1'
    assert settings.EMBEDDING_DIM == 1024
    assert settings.DASHSCOPE_API_KEY == 'vision-only'


def test_embedding_credentials_fall_back_to_legacy_dashscope():
    settings = Settings(DASHSCOPE_API_KEY='legacy', _env_file=None)
    assert settings.embedding_api_key == 'legacy'
    assert settings.embedding_base_url == settings.DASHSCOPE_BASE_URL


async def test_chat_circuit_does_not_block_vision_or_reset_on_vision_success(monkeypatch):
    gateway = build_gateway(monkeypatch)
    gateway._settings.DASHSCOPE_API_KEY = "vision-test"
    stub = WrapperStub(StubModel(AIMessage(content='{"answer":"视觉结果"}')))
    monkeypatch.setattr(gateway, "_vision_model", lambda **kwargs: stub)
    for _ in range(5):
        gateway._record_failure(gateway._settings.CHAT_MODEL)
    result = await gateway.vision_structured(b'image', '识别', Sample)
    assert result.response_metadata['parsed'].answer == '视觉结果'
    with pytest.raises(WorkflowError, match="熔断中"):
        await gateway.chat([HumanMessage(content="hi")])


def test_optional_thinking_setting_is_forwarded_only_when_configured(monkeypatch):
    gateway = build_gateway(monkeypatch)
    default = gateway._chat_model('deepseek-chat', temperature=0.1, max_tokens=100)
    assert default.extra_body is None
    gateway._settings.CHAT_ENABLE_THINKING = False
    compatible = gateway._chat_model('qwen3.8-max', temperature=0.1, max_tokens=100)
    assert compatible.extra_body == {'enable_thinking': False}


async def test_unknown_model_price_is_not_reported_as_free(monkeypatch):
    gateway = build_gateway(monkeypatch)
    monkeypatch.setattr(gateway, '_chat_model', lambda *args, **kwargs: StubModel(AIMessage(content='ok')))
    message = await gateway.chat([HumanMessage(content='hi')], model='unpriced-model')
    assert message.response_metadata['gateway']['cost_estimate_yuan'] is None
