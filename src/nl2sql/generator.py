"""NL2SQL 生成器：检索 schema → 模型生成 SQL → AST 校验 →（至多一次修复）。

修复策略（方案 §6）：
- 语法或字段错误允许携带脱敏错误最多修复一次
- 越权、危险函数与口径不明确不自动重试
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.core.deps import ActorScope
from src.core.config import get_settings
from src.infra.model_gateway import get_model_gateway
from src.modules.analytics.metrics import ANALYTICS_DATASETS, METRICS
from src.nl2sql.plan import validate_semantic_plan
from src.nl2sql.validator import ValidationResult, validate_sql

PROMPT_VERSION = "nl2sql.v1"

# 允许自动修复的校验错误类型
REPAIRABLE_KINDS = {"syntax", "unknown_column"}

SHANGHAI_TZ = timezone(timedelta(hours=8))


@dataclass
class GenerationResult:
    """一次生成（含修复过程）的完整结果。"""

    ok: bool
    sql: str = ""
    normalized_sql: str = ""
    explanation: str = ""
    metric_ids: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    error: str = ""
    validation: ValidationResult | None = None
    repaired: bool = False
    model_calls: int = 0


class SQLPayload(BaseModel):
    """模型输出与业务执行之间的格式契约，禁止静默转换异常字段。"""

    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)

    sql: str = Field(min_length=1, max_length=20000)
    explanation: str = Field(default="", max_length=2000)
    metric_ids: list[str] = Field(default_factory=list, max_length=10)
    assumptions: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("metric_ids")
    @classmethod
    def known_metrics(cls, values: list[str]) -> list[str]:
        if any(value not in METRICS for value in values):
            raise ValueError("模型返回了未开放指标")
        return list(dict.fromkeys(values))


def _schema_text() -> str:
    lines = []
    for table, info in ANALYTICS_DATASETS.items():
        cols = ", ".join(f"{col}（{desc}）" for col, desc in info["columns"].items())
        lines.append(f"- {table}：{info['description']}\n  列：{cols}")
    return "\n".join(lines)


def _metrics_text() -> str:
    lines = []
    for metric in METRICS.values():
        lines.append(f"- {metric.metric_id}（{metric.name}）：{metric.definition}")
    return "\n".join(lines)


def _build_system_prompt(actor: ActorScope) -> str:
    now_shanghai = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M（Asia/Shanghai）")
    shop_scope = actor.shop_ids or []
    return f"""你是电商运营数据查询助手，根据用户问题生成 PostgreSQL 只读查询。

当前时间：{now_shanghai}

授权数据集（只允许访问这些表，禁止其他任何表）：
{_schema_text()}

指标口径（生成时须遵守，可在 metric_ids 中标注使用到的指标）：
{_metrics_text()}

硬性规则：
1. 只生成单条 SELECT 语句；禁止写入、DDL、系统表、危险函数
2. 时间列均为 UTC（timestamptz）。用户说"今天/本周/本月"等按 Asia/Shanghai 解释后换算为 UTC 查询
3. 金额列为最小货币单位"分"（*_minor），结果解释时说明
4. 品类归属使用成交快照列 category
5. 店铺授权范围为 {shop_scope}。租户与店铺授权由服务端逐数据集强制注入，不要生成 tenant_id 字段或授权参数；用户店铺筛选仅能缩小范围。
6. 分析当前库存与销量排名时：销量按 paid_at 归集，禁止用商品更新时间筛选累计销量
7. 查询结果行数会受限，排名类查询请自带 ORDER BY 与合适 LIMIT

只输出一个 JSON 对象（不要多余文字、不要 markdown 代码块），结构：
{{"sql": "SELECT ...", "explanation": "一句话说明查询内容与口径",
  "metric_ids": ["paid_amount"], "assumptions": ["假设说明（如时间范围）"]}}"""


def _sanitize_error(error: str) -> str:
    """错误信息脱敏（截断，避免回灌提示词泄露完整结构）。"""
    return error[:300]


async def _call_model(system_prompt: str, user_prompt: str) -> dict:
    """结构化输出链路：格式由 SQLPayload schema 约束，契约校验（指标白名单）仍在解析时执行。"""
    response = await get_model_gateway().structured(
        [("system", system_prompt), ("user", user_prompt)],
        SQLPayload,
        temperature=0.1,
        max_tokens=1200,
        purpose="nl2sql",
        timeout_seconds=get_settings().SQL_MODEL_TIMEOUT_SECONDS,
        prompt_version=PROMPT_VERSION,
    )
    return response.response_metadata["parsed"].model_dump()


async def generate_sql(
    question: str,
    *,
    actor: ActorScope,
    semantic_plan: dict | None = None,
    max_model_calls: int = 2,
) -> GenerationResult:
    """生成并校验 SQL；语法/字段错误至多自动修复一次。"""
    system_prompt = _build_system_prompt(actor)
    user_prompt = question
    if semantic_plan is not None:
        # 语义计划是生成前的硬门槛：不合法直接拒绝，不消耗模型调用预算。
        plan_check = validate_semantic_plan(semantic_plan)
        if not plan_check.ok:
            return GenerationResult(ok=False, error=f"语义计划不合法：{plan_check.reason}")
        user_prompt += f"\n\n补充语义计划（由运营确认）：{json.dumps(plan_check.normalized_plan, ensure_ascii=False)}"

    if max_model_calls < 1:
        return GenerationResult(ok=False, error="模型调用预算已耗尽")
    prompt = user_prompt
    for attempt in range(min(max_model_calls, 2)):
        try:
            payload = SQLPayload.model_validate(await _call_model(system_prompt, prompt))
        except Exception as exc:
            # 不记录模型输出或校验异常详情，避免把用户内容写入运维日志。
            logger.warning("NL2SQL 输出或调用失败：{}", type(exc).__name__)
            return GenerationResult(ok=False, error="SQL 生成失败，请检查模型配置或稍后重试",
                                    repaired=attempt > 0, model_calls=attempt + 1)
        validation = validate_sql(payload.sql)
        if validation.ok:
            return GenerationResult(
                ok=True, sql=payload.sql, normalized_sql=validation.normalized_sql,
                explanation=payload.explanation, metric_ids=payload.metric_ids,
                assumptions=payload.assumptions, validation=validation,
                repaired=attempt > 0, model_calls=attempt + 1,
            )
        failure = GenerationResult(ok=False, sql=payload.sql, error=validation.reason,
                                   validation=validation, repaired=attempt > 0, model_calls=attempt + 1)
        if validation.error_kind not in REPAIRABLE_KINDS or attempt + 1 >= min(max_model_calls, 2):
            return failure
        # 修复保留原始语义计划，且仅发生一次；越权和危险函数不走此分支。
        prompt = (
            f"{user_prompt}\n\n上一次生成失败。\n失败 SQL：{payload.sql}\n"
            f"错误信息：{_sanitize_error(validation.reason)}\n请修复后重新输出完整 JSON。"
        )
    raise AssertionError("不可达的生成状态")
