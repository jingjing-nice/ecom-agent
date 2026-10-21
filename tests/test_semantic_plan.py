"""语义计划校验（方案 §6）：SQL 生成前的口径硬门槛。

覆盖：
- 指标开放范围、维度口径授权、归集时间字段混算与画像维度拒绝
- UTC 半开时间区间的格式与时区校验、过滤字段白名单
- 生成器集成：非法计划直接拒绝且不消耗模型调用预算；合法计划规范化后进入提示词
"""
from unittest.mock import AsyncMock

import pytest

from src.core.deps import ActorScope
from src.nl2sql.generator import generate_sql
from src.nl2sql.plan import validate_semantic_plan

ACTOR = ActorScope("ops", "tenant", "ops", ["shop"])


def plan(**overrides):
    base = {
        "metric_ids": ["paid_units"],
        "dimensions": ["category"],
        "time_range": {"start": "2026-10-01T00:00:00+08:00", "end": "2026-10-18T00:00:00+08:00"},
    }
    base.update(overrides)
    return base


def model_payload(sql="SELECT SUM(quantity) FROM fact_order_items"):
    return {"sql": sql, "metric_ids": ["paid_units"]}


def test_valid_plan_normalizes_time_to_utc():
    check = validate_semantic_plan(plan())
    assert check.ok, check.reason
    # Asia/Shanghai 时间解释后换算为 UTC 半开区间
    assert check.normalized_plan["time_range"] == {
        "start": "2026-09-30T16:00:00+00:00",
        "end": "2026-10-17T16:00:00+00:00",
    }
    assert check.normalized_plan["metric_ids"] == ["paid_units"]


def test_same_time_field_metrics_can_combine():
    check = validate_semantic_plan({"metric_ids": ["paid_amount", "paid_units"], "dimensions": ["category"]})
    assert check.ok, check.reason


def test_snapshot_metric_without_time_range_is_valid():
    check = validate_semantic_plan({"metric_ids": ["available_inventory"]})
    assert check.ok and "time_range" not in check.normalized_plan


@pytest.mark.parametrize("bad, reason_part", [
    (None, "必须是 JSON 对象"),
    ([], "必须是 JSON 对象"),
    ({}, "格式不合法"),
    ({"metric_ids": []}, "格式不合法"),
    ({"metric_ids": ["paid_units"], "unknown_field": 1}, "格式不合法"),
    ({"metric_ids": ["ghost_metric"]}, "未开放指标"),
    ({"metric_ids": ["paid_units"], "dimensions": ["region"]}, "画像维度"),
    ({"metric_ids": ["paid_units"], "dimensions": ["age_group"]}, "画像维度"),
    ({"metric_ids": ["paid_units"], "dimensions": ["hostname"]}, "未知维度"),
    # paid_orders 的开放维度仅 shop_id/date
    ({"metric_ids": ["paid_orders"], "dimensions": ["category"]}, "未开放维度"),
    # 支付类（paid_at）与退款类（completed_at）归集口径不同，禁止混算
    ({"metric_ids": ["paid_amount", "refund_amount"]}, "不能混算"),
    ({"metric_ids": ["paid_units"], "filters": {"limit": "10"}}, "不支持的过滤字段"),
    ({"metric_ids": ["paid_units"], "filters": {"category": " "}}, "过滤值不能为空"),
    ({"metric_ids": ["paid_units"], "time_range": {"start": "2026-10-01", "end": "2026-10-18"}}, "必须携带时区"),
    ({"metric_ids": ["paid_units"], "time_range": {"start": "not-a-date+08:00", "end": "2026-10-18T00:00:00+08:00"}}, "不是合法"),
    ({"metric_ids": ["paid_units"], "time_range": {"start": "2026-10-18T00:00:00+08:00", "end": "2026-10-01T00:00:00+08:00"}}, "半开区间"),
])
def test_invalid_plans_are_rejected(bad, reason_part):
    check = validate_semantic_plan(bad)
    assert not check.ok
    assert reason_part in check.reason


async def test_invalid_plan_rejected_without_model_calls(monkeypatch):
    model = AsyncMock()
    monkeypatch.setattr("src.nl2sql.generator._call_model", model)
    result = await generate_sql("支付件数", actor=ACTOR, semantic_plan={"metric_ids": ["ghost_metric"]})
    assert not result.ok and result.model_calls == 0
    assert "语义计划不合法" in result.error
    model.assert_not_awaited()


async def test_normalized_plan_enters_prompt(monkeypatch):
    model = AsyncMock(return_value=model_payload())
    monkeypatch.setattr("src.nl2sql.generator._call_model", model)
    result = await generate_sql("支付件数", actor=ACTOR, semantic_plan=plan())
    assert result.ok
    prompt = model.call_args.args[1]
    assert '"metric_ids": ["paid_units"]' in prompt
    assert '"start": "2026-09-30T16:00:00+00:00"' in prompt