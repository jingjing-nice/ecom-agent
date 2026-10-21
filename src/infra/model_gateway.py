"""模型网关：统一 Chat / Embedding / Vision Provider 接口。

职责（方案 §9）：
- 记录模型 ID、prompt_version、输入输出 token、耗时、费用与错误
- 主模型超时或限流时触发熔断；备用模型通过评测后才切换
- 传输层一律使用 LangChain Runnable（ChatDeepSeek / ChatOpenAI / OpenAIEmbeddings），
  不直接持有 Provider SDK 客户端；计量、熔断与降级统一收敛在本网关。

Chat 出入口均为 LangChain 消息：chat() 直接返回 AIMessage（含 usage_metadata 与
tool_calls），计量信息挂在 response_metadata["gateway"]，调用方不再做
OpenAI 字典 / JSON 字符串往返转换。
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.messages.utils import convert_to_messages
from langchain_core.runnables import Runnable
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pydantic import BaseModel
from loguru import logger

from src.core.config import get_settings
from src.core.exceptions import WorkflowError

# 估算单价（元 / 百万 token），仅用于成本口径展示，按实际账单校准
_PRICE_PER_MILLION = {
    "deepseek-chat": {"input": 2.0, "output": 8.0},
    "qwen-vl-max": {"input": 3.0, "output": 9.0},
}
_EMBED_PRICE_PER_MILLION = 0.5

SchemaT = TypeVar("SchemaT", bound=BaseModel)


@dataclass
class _CircuitState:
    """极简熔断器：连续失败达到阈值后打开一段时间。"""

    failures: int = 0
    opened_at: float = 0.0


class ModelGateway:
    """统一模型接入层。"""

    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._circuits: dict[str, _CircuitState] = {}
        self._circuit_threshold = 5
        self._circuit_open_seconds = 60

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------
    def _circuit_state(self, model: str | None = None) -> _CircuitState:
        return self._circuits.setdefault(model or self._settings.CHAT_MODEL, _CircuitState())

    def _circuit_open(self, model: str | None = None) -> bool:
        circuit = self._circuit_state(model)
        if circuit.failures < self._circuit_threshold:
            return False
        if time.monotonic() - circuit.opened_at > self._circuit_open_seconds:
            circuit.failures = self._circuit_threshold - 1
            return False
        return True

    def _record_failure(self, model: str | None = None) -> None:
        circuit = self._circuit_state(model)
        circuit.failures += 1
        if circuit.failures >= self._circuit_threshold:
            circuit.opened_at = time.monotonic()
            logger.warning("模型网关熔断打开：model={}，暂停调用 60 秒", model or self._settings.CHAT_MODEL)

    def _record_success(self, model: str | None = None) -> None:
        self._circuit_state(model).failures = 0

    def _chat_model(self, model: str, *, temperature: float, max_tokens: int) -> ChatDeepSeek:
        """LangChain ChatModel：ChatDeepSeek 是唯一的文本 Chat 传输层。"""
        return ChatDeepSeek(
            model=model,
            api_key=self._settings.DEEPSEEK_API_KEY,
            base_url=self._settings.DEEPSEEK_BASE_URL,
            temperature=temperature,
            max_tokens=max_tokens,
            max_retries=0,
            timeout=self._settings.CHAT_TRANSPORT_TIMEOUT_SECONDS,
            stream_usage=True,
            extra_body=({"enable_thinking": self._settings.CHAT_ENABLE_THINKING}
                        if self._settings.CHAT_ENABLE_THINKING is not None else None),
        )

    def _vision_model(self, *, max_tokens: int) -> ChatOpenAI:
        """视觉传输层：DashScope 的 OpenAI 兼容端点（LangChain ChatOpenAI）。"""
        return ChatOpenAI(
            model=self._settings.VISION_MODEL,
            api_key=self._settings.DASHSCOPE_API_KEY or "not-configured",
            base_url=self._settings.DASHSCOPE_BASE_URL,
            temperature=0.1,
            max_tokens=max_tokens,
            max_retries=0,
            timeout=45,
        )

    def chat_runnable(self, *, tools: list[Any] | None = None, model: str | None = None,
                      temperature: float = 0.2, max_tokens: int = 2048):
        """返回可 ainvoke / astream 的 LangChain Runnable，供热式流式输出使用。"""
        if not self._settings.DEEPSEEK_API_KEY:
            raise WorkflowError("未配置主模型 API Key")
        if self._circuit_open(model):
            raise WorkflowError("模型服务暂时不可用（熔断中），请稍后重试")
        runnable = self._chat_model(model or self._settings.CHAT_MODEL, temperature=temperature, max_tokens=max_tokens)
        if tools:
            runnable = runnable.bind_tools(tools)
        return runnable

    def _finish(self, message: AIMessage, *, model: str, purpose: str,
                 prompt_version: str, start: float, kind: str = "chat") -> AIMessage:
        """成功后的统一计量：token/耗时/费用写入 response_metadata.gateway 并记日志。"""
        self._record_success(model)
        latency_ms = int((time.perf_counter() - start) * 1000)
        usage = message.usage_metadata or {}
        prompt_tokens = int(usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("output_tokens") or 0)
        price = _PRICE_PER_MILLION.get(model)
        cost = ((prompt_tokens * price["input"] + completion_tokens * price["output"]) / 1_000_000
                if price else None)
        cost_label = f"{cost:.4f}元" if cost is not None else "unknown (price not configured)"
        logger.info(
            f"[model] {kind} purpose={purpose} prompt_version={prompt_version} "
            f"model={model} in={prompt_tokens} out={completion_tokens} "
            f"latency={latency_ms}ms cost~{cost_label}"
        )
        message.response_metadata = {
            **(message.response_metadata or {}),
            "gateway": {"model": model, "latency_ms": latency_ms,
                        "cost_estimate_yuan": round(cost, 6) if cost is not None else None},
        }
        return message

    async def chat(
        self,
        messages: list[dict[str, Any]] | list[BaseMessage],
        *,
        tools: list[Any] | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        purpose: str = "generic",
        prompt_version: str = "",
        timeout_seconds: float = 30.0,
    ) -> AIMessage:
        """调用 Chat 模型，直接返回 LangChain AIMessage（含 usage 与 gateway 计量）。

        messages 接受 LangChain 消息或 OpenAI 风格字典；tool_calls 为标准
        LangChain 结构（name/args/id），由调用方按服务端白名单校验后执行。
        """
        use_model = model or self._settings.CHAT_MODEL
        runnable = self.chat_runnable(tools=tools, model=use_model, temperature=temperature, max_tokens=max_tokens)

        start = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                runnable.ainvoke(convert_to_messages(messages)),
                timeout=timeout_seconds,
            )
        except Exception as exc:
            self._record_failure(use_model)
            logger.error(f"模型调用失败 purpose={purpose} model={use_model}: {exc}")
            raise WorkflowError("模型调用失败，请稍后重试") from exc

        if response.invalid_tool_calls:
            raise WorkflowError("模型返回了无效的工具参数")
        if len({call.get("id") for call in (response.tool_calls or [])}) != len(response.tool_calls or []):
            raise WorkflowError("模型返回了重复的工具调用 ID")
        return self._finish(response, model=use_model, purpose=purpose, prompt_version=prompt_version, start=start)

    async def structured(
        self,
        messages: list[dict[str, Any]] | list[BaseMessage],
        schema: type[SchemaT],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        purpose: str = "generic",
        prompt_version: str = "",
        timeout_seconds: float = 30.0,
        clean: "Callable[[SchemaT], SchemaT] | None" = None,
        runnable_override: Runnable | None = None,
    ) -> AIMessage:
        """Schema 约束的结构化输出：官方 with_structured_output 解析链路 + 网关计量。

        返回的 AIMessage 携带 response_metadata["parsed"]（schema 实例）；服务端
        契约校验（model_validator）仍在解析时执行。解析失败不静默降级，
        抛 WorkflowError，由调用方决定重试策略（如语义切片的带错重试）。
        clean 用于对非核心字段做容错清洗（如丢弃非法 bbox），不改变 schema 语义。
        """
        use_model = model or self._settings.CHAT_MODEL
        if runnable_override is not None:
            runnable = runnable_override
            if self._circuit_open(use_model):
                raise WorkflowError("模型服务暂时不可用（熔断中），请稍后重试")
        else:
            runnable = self.chat_runnable(model=use_model, temperature=temperature, max_tokens=max_tokens)
        chain: Runnable = runnable.with_structured_output(schema, include_raw=True)

        start = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                chain.ainvoke(convert_to_messages(messages)),
                timeout=timeout_seconds,
            )
        except Exception as exc:
            self._record_failure(use_model)
            logger.error(f"结构化模型调用失败 purpose={purpose} model={use_model}: {exc}")
            raise WorkflowError("模型调用失败，请稍后重试") from exc

        parsed, message = result["parsed"], result["raw"]
        if parsed is None:
            self._record_failure(use_model)
            error = result.get("parsing_error") or ""
            raise WorkflowError(f"模型未返回符合 schema 的结果：{str(error)[:200]}")
        if clean is not None:
            parsed = clean(parsed)
        message = self._finish(message, model=use_model, purpose=purpose, prompt_version=prompt_version,
                               start=start, kind="structured")
        message.response_metadata = {**(message.response_metadata or {}), "parsed": parsed}
        return message

    # ------------------------------------------------------------------
    # Chat（流式）
    # ------------------------------------------------------------------
    async def astream_chat(
        self,
        messages: list[dict[str, Any]] | list[BaseMessage],
        *,
        tools: list[Any] | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        purpose: str = "generic",
        prompt_version: str = "",
        timeout_seconds: float = 60.0,
    ) -> AsyncIterator[str]:
        """流式 Chat：逐块产出文本，完成后统一计量（与 chat 同口径）。

        熔断、错误类型与 chat() 保持一致；消费方提前关闭生成器不计为失败。
        """
        use_model = model or self._settings.CHAT_MODEL
        runnable = self.chat_runnable(tools=tools, model=use_model, temperature=temperature, max_tokens=max_tokens)

        start = time.perf_counter()
        prompt_tokens = completion_tokens = 0
        try:
            async with asyncio.timeout(timeout_seconds):
                async for chunk in runnable.astream(convert_to_messages(messages)):
                    usage = chunk.usage_metadata
                    if usage:
                        prompt_tokens = int(usage.get("input_tokens") or prompt_tokens)
                        completion_tokens = int(usage.get("output_tokens") or completion_tokens)
                    text = chunk.content if isinstance(chunk.content, str) else ""
                    if text:
                        yield text
        except TimeoutError as exc:
            self._record_failure(use_model)
            logger.error(f"模型流式调用超时 purpose={purpose} model={use_model}")
            raise WorkflowError("模型调用超时，请稍后重试") from exc
        except Exception as exc:
            self._record_failure(use_model)
            logger.error(f"模型流式调用失败 purpose={purpose} model={use_model}: {exc}")
            raise WorkflowError("模型调用失败，请稍后重试") from exc

        self._finish(AIMessage(content="", usage_metadata={
            "input_tokens": prompt_tokens, "output_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens}), model=use_model,
            purpose=purpose, prompt_version=prompt_version, start=start, kind="stream")

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------
    def _embedding_model(self) -> OpenAIEmbeddings:
        """LangChain OpenAIEmbeddings 指向 DashScope 兼容端点；官方 chunk_size 分批。"""
        if not self._settings.embedding_api_key:
            raise WorkflowError("未配置 Embedding API Key")
        return OpenAIEmbeddings(
            model=self._settings.EMBEDDING_MODEL,
            api_key=self._settings.embedding_api_key,
            base_url=self._settings.embedding_base_url,
            dimensions=self._settings.EMBEDDING_DIM,
            chunk_size=10,  # DashScope 单请求上限
            check_embedding_ctx_length=False,  # 中文文本不做 tiktoken 预切
            max_retries=2,
        )

    async def embed(self, texts: list[str], *, purpose: str = "rag") -> list[list[float]]:
        """批量向量化（官方集成内部按 10 条分批），返回前后做维度与数值校验。"""
        if not texts:
            return []

        start = time.perf_counter()
        vectors = await self._embedding_model().aembed_documents(texts)
        if len(vectors) != len(texts):
            raise WorkflowError("Embedding 返回数量不完整")
        if any(len(vector) != self._settings.EMBEDDING_DIM
               or any(not math.isfinite(value) for value in vector) for vector in vectors):
            raise WorkflowError("Embedding 返回维度或数值与配置不一致")

        latency_ms = int((time.perf_counter() - start) * 1000)
        total_tokens = sum(len(t) for t in texts)  # 近似口径
        cost = total_tokens * _EMBED_PRICE_PER_MILLION / 1_000_000
        logger.info(
            f"[model] embed purpose={purpose} model={self._settings.EMBEDDING_MODEL} "
            f"texts={len(texts)} latency={latency_ms}ms cost~{cost:.4f}元"
        )
        return vectors

    # ------------------------------------------------------------------
    # Vision（商品图片识别 / 凭证 OCR）
    # ------------------------------------------------------------------
    async def vision_structured(
        self,
        image_bytes: bytes,
        prompt: str,
        schema: type[SchemaT],
        *,
        mime_type: str = "image/jpeg",
        max_tokens: int = 1500,
        purpose: str = "vision",
        timeout_seconds: float = 45.0,
        clean: "Callable[[SchemaT], SchemaT] | None" = None,
    ) -> AIMessage:
        """视觉结构化提取：ChatOpenAI 多模态消息 + with_structured_output 官方解析链路。

        复杂视觉任务走独立预算，不与文本任务混算；返回同 structured()。
        """
        if not self._settings.DASHSCOPE_API_KEY:
            raise WorkflowError("未配置视觉模型 API Key")
        if self._circuit_open(self._settings.VISION_MODEL):
            raise WorkflowError("模型服务暂时不可用（熔断中），请稍后重试")
        encoded = base64.b64encode(image_bytes).decode("ascii")
        message = HumanMessage(content=[
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}},
        ])
        return await self.structured(
            [message], schema,
            model=self._settings.VISION_MODEL,
            purpose=purpose, timeout_seconds=timeout_seconds, clean=clean,
            max_tokens=max_tokens, temperature=0.1,
            runnable_override=self._vision_model(max_tokens=max_tokens),
        )


_gateway: ModelGateway | None = None


def get_model_gateway() -> ModelGateway:
    """进程级单例模型网关。"""
    global _gateway
    if _gateway is None:
        _gateway = ModelGateway()
    return _gateway
