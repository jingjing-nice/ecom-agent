"""LangChain ChatModel 薄适配：把 create_agent / 中间件的模型调用收敛到 AgentRuntime。

- 预算（TASK_MAX_MODEL_CALLS）与网关计量由 AgentRuntime.model / model_stream 提供，
  适配器不持密钥、不发请求，模型调用不得绕过网关（方案 §9）。
- _astream 逐块转发网关流，供 create_agent 以 stream_mode="messages" 真流式输出；
  _agenerate 供非流式调用（与既有测试桩 call_agent_model 同一入口）。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult


class GatewayChatModel(BaseChatModel):
    """经 AgentRuntime 调用模型网关的 ChatModel 适配器（不直连 Provider）。"""

    runtime: Any = None
    purpose: str = "agent"
    tools: list[Any] = []

    @property
    def _llm_type(self) -> str:
        return "ecom-gateway"

    def bind_tools(self, tools, **kwargs):
        """create_agent 在构建时绑定工具；保持自身实例以复用预算与流式实现。"""
        return self.model_copy(update={"tools": list(tools)})

    def _generate(self, *args, **kwargs):
        raise NotImplementedError("GatewayChatModel 仅支持异步调用")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        response = await self.runtime.model(messages, self.tools, purpose=self.purpose)
        response.response_metadata = {**(response.response_metadata or {}), "purpose": self.purpose}
        return ChatResult(generations=[ChatGeneration(message=response)])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs) -> AsyncIterator[ChatGenerationChunk]:
        stream = await self.runtime.model_stream(messages, self.tools, purpose=self.purpose)
        async for text in stream:
            yield ChatGenerationChunk(message=AIMessageChunk(
                content=text, response_metadata={"purpose": self.purpose}))


class RulesDecisionModel(BaseChatModel):
    """规则降级决策模型：不调用外部 Provider，由回调给出确定性工具决策（零模型成本路径）。

    供 create_agent 在 AGENT_ROUTING_MODE=rules 下复用同一套官方循环与中间件护栏；
    decide 每轮基于最新的 runtime.outputs 重新计算，工具执行后自然推进/终止。
    """

    decide: Any = None

    @property
    def _llm_type(self) -> str:
        return "ecom-rules"

    def bind_tools(self, tools, **kwargs):
        """工具白名单由 runtime 服务端校验，规则决策不消费绑定。"""
        return self

    def _generate(self, *args, **kwargs):
        raise NotImplementedError("RulesDecisionModel 仅支持异步调用")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self.decide())])