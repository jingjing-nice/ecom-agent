"""LangChain 层：模型输出契约与结构化输出归属。

职责边界（LangChain 侧）：
- AgentResult 是子 Agent / Supervisor 面向用户的结构化输出契约。
- 结构化解析、Schema 暴露与校验集中在此，图路由与状态机不得自行解析模型文案。
- 卡片、证据、待确认状态与数字一律来自已校验的工具结果，禁止从模型生成文案反解析。
"""
from __future__ import annotations

from typing import Any

from src.agents.contracts import AgentResult


def result_schema() -> dict[str, Any]:
    """供 with_structured_output / response_format 使用的 JSON Schema。"""
    return AgentResult.model_json_schema()


def coerce_result(payload: dict[str, Any] | AgentResult) -> AgentResult:
    """把结构化负载按契约校验为 AgentResult；非结构化输入直接拒绝。"""
    if isinstance(payload, AgentResult):
        return payload
    return AgentResult.model_validate(payload)


def dump_result(payload: dict[str, Any] | AgentResult) -> dict[str, Any]:
    """校验后序列化，作为进入图状态与前端下发的唯一入口。"""
    return coerce_result(payload).model_dump(mode="json")
