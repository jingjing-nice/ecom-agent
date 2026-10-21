from unittest.mock import AsyncMock
import pytest
from src.core.deps import ActorScope
from src.nl2sql.generator import generate_sql

ACTOR = ActorScope("ops", "tenant", "ops", ["shop"])


def payload(sql="SELECT SUM(quantity) FROM fact_order_items", **values):
    return {"sql": sql, "metric_ids": ["paid_units"], "assumptions": [], **values}


@pytest.mark.parametrize("bad", [
    None, [], {"sql": None}, payload(metric_ids=None), payload(assumptions="no"),
    payload(metric_ids=["nonexistent_metric"]), payload(explanation={"text": "wrong type"}),
])
async def test_malformed_model_payload_fails_without_uncaught_errors(monkeypatch, bad):
    model = AsyncMock(return_value=bad)
    monkeypatch.setattr("src.nl2sql.generator._call_model", model)
    result = await generate_sql("支付件数", actor=ACTOR)
    assert not result.ok
    assert result.model_calls == 1
    assert model.await_count == 1


async def test_repairs_once_and_preserves_semantic_plan(monkeypatch):
    model = AsyncMock(side_effect=[payload("SELECT missing FROM fact_order_items"), payload()])
    monkeypatch.setattr("src.nl2sql.generator._call_model", model)
    result = await generate_sql("支付件数", actor=ACTOR, semantic_plan={"metric_ids": ["paid_units"]})
    assert result.ok and result.repaired and result.model_calls == 2
    assert '"metric_ids": ["paid_units"]' in model.call_args_list[1].args[1]


@pytest.mark.parametrize("sql", ["SELECT pg_sleep(1)", "SELECT * FROM users"])
async def test_dangerous_or_unauthorized_sql_is_never_repaired(monkeypatch, sql):
    model = AsyncMock(return_value=payload(sql))
    monkeypatch.setattr("src.nl2sql.generator._call_model", model)
    result = await generate_sql("问题", actor=ACTOR)
    assert not result.ok and not result.repaired
    assert model.await_count == 1


async def test_budget_one_disables_repair(monkeypatch):
    model = AsyncMock(return_value=payload("SELECT missing FROM fact_order_items"))
    monkeypatch.setattr("src.nl2sql.generator._call_model", model)
    result = await generate_sql("问题", actor=ACTOR, max_model_calls=1)
    assert not result.ok and result.model_calls == 1
    assert model.await_count == 1


async def test_exhausted_budget_does_not_call_provider(monkeypatch):
    model = AsyncMock()
    monkeypatch.setattr("src.nl2sql.generator._call_model", model)
    result = await generate_sql("问题", actor=ACTOR, max_model_calls=0)
    assert not result.ok and result.model_calls == 0
    model.assert_not_awaited()


def test_prompt_pins_authorization_and_timezone_rules():
    from src.nl2sql.generator import _build_system_prompt
    prompt = _build_system_prompt(ACTOR)
    assert "Asia/Shanghai" in prompt and "shop" in prompt and "SELECT" in prompt
