"""图状态及子 Agent 输出契约。数据库会话和授权主体不进入模型状态。

职责划分：
- LangChain 负责模型调用、工具与结构化输出（见 src/agents/structured.py）。
- LangGraph 负责路由、状态、人工审批与恢复（见 supervisor / subagents / checkpoint）。
"""
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

AgentName = Literal["shopping", "aftersale", "analytics"]


class AgentResult(BaseModel):
    agent: AgentName
    message: str
    cards: list[dict] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)
    handoff: dict | None = None
    pending_confirmation: bool = False
    data: dict | None = None


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    rounds: int
    result: dict


class SupervisorState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    rounds: int
    results: list[dict]
    answer: dict
    approval: dict
    visited: list[str]
    runtime: dict
    owner: dict
    request: dict
