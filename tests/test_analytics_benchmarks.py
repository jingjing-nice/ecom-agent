"""人工基准 SQL（方案 §6）：10 指标全覆盖、通过 AST 校验且口径要点正确。"""
import pytest

from src.modules.analytics.benchmarks import BENCHMARKS, benchmark_for
from src.modules.analytics.metrics import METRICS
from src.nl2sql.validator import validate_sql


def test_benchmarks_cover_all_metrics():
    assert set(BENCHMARKS) == set(METRICS)


def test_benchmark_lookup():
    assert benchmark_for("paid_amount") is BENCHMARKS["paid_amount"]
    assert benchmark_for("ghost_metric") is None


@pytest.mark.parametrize("metric_id", sorted(BENCHMARKS))
def test_benchmark_passes_ast_validation(metric_id):
    benchmark = BENCHMARKS[metric_id]
    result = validate_sql(benchmark.sql)
    assert result.ok, result.reason
    assert "LIMIT" in result.normalized_sql.upper()
    # 画像维度在保护完成前不得出现
    assert "region" not in benchmark.sql
    assert "age_group" not in benchmark.sql


@pytest.mark.parametrize("metric_id", sorted(BENCHMARKS))
def test_benchmark_time_field_matches_metric_definition(metric_id):
    benchmark = BENCHMARKS[metric_id]
    if benchmark.time_field is None:
        # 快照类指标不做时间窗口过滤
        assert "observed_at" not in benchmark.sql
        return
    assert benchmark.time_field == METRICS[metric_id].time_field
    # UTC 半开区间 [start, end)
    assert f"{benchmark.time_field} >= '" in benchmark.sql
    assert f"{benchmark.time_field} < '" in benchmark.sql


def test_refund_benchmarks_use_completed_at_separately():
    for metric_id in ("refund_amount", "refund_units"):
        sql = BENCHMARKS[metric_id].sql
        assert "fact_refunds" in sql and "completed_at" in sql
        # 退款不与支付混算
        assert "paid_at" not in sql and "fact_order_items" not in sql


def test_avg_order_value_guards_division_by_zero():
    assert "NULLIF" in BENCHMARKS["avg_order_value"].sql


def test_rank_benchmarks_include_zero_sales():
    for metric_id in ("product_sales_rank", "category_sales_rank"):
        benchmark = BENCHMARKS[metric_id]
        assert benchmark.include_zero
        assert "LEFT JOIN" in benchmark.sql
        assert "COALESCE" in benchmark.sql


def test_low_stock_benchmark_documents_threshold():
    benchmark = BENCHMARKS["low_stock_skus"]
    assert "available_qty < 10" in benchmark.sql
    assert "阈值" in benchmark.notes