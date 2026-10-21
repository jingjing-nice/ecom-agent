"""保留现有网关的计量与熔断，使用 LangChain 的消息及工具标准。

LangChain 负责模型与工具：LangChain 消息与 BaseTool 直接交给网关，网关直接返回
AIMessage（tool_calls 为标准 LangChain 结构），不再做 OpenAI 字典 / JSON 字符串往返；
计量、熔断与降级仍由模型网关统一提供。
流式合成（astream）同样经网关计量，模型调用不得绕过此处直连 SDK。
"""
from collections.abc import AsyncIterator

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool

from src.infra.model_gateway import get_model_gateway


async def call_agent_model(messages: list[BaseMessage], tools: list[BaseTool], *, purpose: str) -> AIMessage:
    """Agent 决策模型调用：入出参均为 LangChain 消息，计量与工具合法性校验在网关完成。"""
    return await get_model_gateway().chat(
        messages,
        tools=list(tools),
        purpose=purpose, prompt_version="langgraph.v1", max_tokens=1600,
    )


async def stream_agent_model(messages: list[BaseMessage], tools: list[BaseTool], *, purpose: str) -> AsyncIterator[str]:
    """流式调用模型，逐块产出文本（用于答复合成的真流式输出）。"""
    async for text in get_model_gateway().astream_chat(
        messages, tools=list(tools), purpose=purpose, prompt_version="langgraph.v1", max_tokens=1600,
    ):
        yield text
