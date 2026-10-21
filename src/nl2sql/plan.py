"""分析语义计划校验（方案 §6）。

语义计划由运营确认或上层服务端构造后传入生成器，是 SQL 生成前的硬门槛：
不通过时直接拒绝，不消耗模型调用预算。校验覆盖指标开放范围、维度的口径
授权、UTC 半开时间区间与过滤字段白名单。

口径约束：
- 归集时间字段不同的指标不能混算（如支付按 paid_at、退款按 completed_at）
- 画像维度（region/age_group）在匿名计数与小群组隐藏完成前保持拒绝
- 过滤只能进一步缩小授权范围，不能扩大
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from src.modules.analytics.metrics import DIMENSIONS, METRICS

# 计划允许的过滤键（服务端注入的租户与店铺范围之外的可选收窄条件）
ALLOWED_FILTERS = {"category", "brand", "shop_id"}
# 小群组保护完成前拒绝的画像维度
BLOCKED_DIMENSIONS = {"region", "age_group"}


class PlanTimeRange(BaseModel):
    """UTC 半开时间区间 [start, end)。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    start: str
    end: str


class SemanticPlan(BaseModel):
    """语义计划契约：指标 + 维度 + 时间区间 + 可选过滤。禁止静默转换。"""

    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)

    metric_ids: list[str] = Field(min_length=1, max_length=10)
    dimensions: list[str] = Field(default_factory=list, max_length=10)
    time_range: PlanTimeRange | None = None
    filters: dict[str, str] = Field(default_factory=dict)


@dataclass
class PlanValidation:
    """校验结果；通过时 normalized_plan 为规范化后的 UTC 计划。"""

    ok: bool
    reason: str = ""
    normalized_plan: dict | None = None


def _to_utc(value: str, label: str) -> datetime | str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return f"{label}不是合法 ISO 8601 时间"
    if parsed.tzinfo is None:
        return f"{label}必须携带时区，查询统一换算为 UTC 半开区间"
    return parsed.astimezone(timezone.utc)


def validate_semantic_plan(plan: dict | None) -> PlanValidation:
    """校验语义计划；失败时返回受控原因，不暴露模型或内部细节。"""
    if not isinstance(plan, dict):
        return PlanValidation(ok=False, reason="语义计划必须是 JSON 对象")
    try:
        parsed = SemanticPlan.model_validate(plan)
    except Exception:
        return PlanValidation(ok=False, reason="语义计划格式不合法")

    metric_ids = list(dict.fromkeys(parsed.metric_ids))
    unknown = [metric_id for metric_id in metric_ids if metric_id not in METRICS]
    if unknown:
        return PlanValidation(ok=False, reason=f"未开放指标：{'、'.join(unknown)}")
    # 归集时间字段必须一致，否则同一查询无法按统一窗口正确归集（如支付与退款混算）。
    time_fields = {METRICS[metric_id].time_field for metric_id in metric_ids}
    if len(time_fields) > 1:
        return PlanValidation(ok=False, reason="归集时间字段不同的指标不能混算，请拆分查询")

    dimensions = list(dict.fromkeys(parsed.dimensions))
    blocked = [dim for dim in dimensions if dim in BLOCKED_DIMENSIONS]
    if blocked:
        return PlanValidation(ok=False, reason="画像维度在匿名计数与小群组隐藏完成前保持拒绝")
    for dim in dimensions:
        if dim not in DIMENSIONS:
            return PlanValidation(ok=False, reason=f"未知维度：{dim}")
        for metric_id in metric_ids:
            if dim not in METRICS[metric_id].allowed_dimensions:
                return PlanValidation(ok=False, reason=f"指标 {metric_id} 未开放维度 {dim}")

    invalid_filters = sorted(key for key in parsed.filters if key not in ALLOWED_FILTERS)
    if invalid_filters:
        return PlanValidation(ok=False, reason=f"不支持的过滤字段：{'、'.join(invalid_filters)}")
    if any(not value for value in parsed.filters.values()):
        return PlanValidation(ok=False, reason="过滤值不能为空")

    normalized: dict = {
        "metric_ids": metric_ids,
        "dimensions": dimensions,
        "filters": dict(parsed.filters),
    }
    if parsed.time_range is not None:
        start = _to_utc(parsed.time_range.start, "时间起点")
        if isinstance(start, str):
            return PlanValidation(ok=False, reason=start)
        end = _to_utc(parsed.time_range.end, "时间终点")
        if isinstance(end, str):
            return PlanValidation(ok=False, reason=end)
        if start >= end:
            return PlanValidation(ok=False, reason="时间区间必须满足 start < end（UTC 半开区间）")
        normalized["time_range"] = {"start": start.isoformat(), "end": end.isoformat()}
    return PlanValidation(ok=True, normalized_plan=normalized)