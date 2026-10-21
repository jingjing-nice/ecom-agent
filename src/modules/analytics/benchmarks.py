"""人工基准 SQL（方案 §6）：10 个首批指标的权威参考写法。

用途：口径对照与回归评测的人工基准。全部基准必须通过 `validate_sql`
硬校验，并覆盖以下口径要点：
- 支付类指标按 paid_at 归集，UTC 半开区间 [start, end)
- 退款类指标按 completed_at 归集，不与支付金额混算
- 客单价分母用 NULLIF 防除零
- 排名类含零销量项（LEFT JOIN + COALESCE(..., 0)）
- 不出现画像维度（region/age_group），小群组保护完成前保持拒绝

时间窗为演示沙箱固定字面量（UTC 半开，覆盖种子数据）；实际执行时由
服务端按语义计划换算时间，本模块只作权威参考，不直接执行。
"""
from __future__ import annotations

from dataclasses import dataclass

# 演示沙箱固定窗口（UTC）：2026-10-01 至 2026-10-18，种子支付数据在窗口内。
WINDOW = "'2026-10-01 00:00:00+00' AND {field} < '2026-10-18 00:00:00+00'"


def _windowed(field: str) -> str:
    return f"{field} >= " + WINDOW.format(field=field)


@dataclass(frozen=True)
class Benchmark:
    """单指标人工基准：SQL 与口径要点元数据。"""

    metric_id: str
    sql: str
    # 归集时间字段；快照类指标为 None（不对时间做窗口过滤）
    time_field: str | None = None
    include_zero: bool = False
    notes: str = ""


BENCHMARKS: dict[str, Benchmark] = {
    "paid_amount": Benchmark(
        metric_id="paid_amount",
        time_field="paid_at",
        sql=(
            "SELECT category, SUM(paid_amount_minor) AS paid_amount_minor\n"
            "FROM fact_order_items\n"
            f"WHERE {_windowed('paid_at')}\n"
            "GROUP BY category\n"
            "ORDER BY paid_amount_minor DESC\n"
            "LIMIT 10"
        ),
    ),
    "paid_units": Benchmark(
        metric_id="paid_units",
        time_field="paid_at",
        sql=(
            "SELECT category, SUM(quantity) AS paid_units\n"
            "FROM fact_order_items\n"
            f"WHERE {_windowed('paid_at')}\n"
            "GROUP BY category\n"
            "ORDER BY paid_units DESC\n"
            "LIMIT 10"
        ),
    ),
    "paid_orders": Benchmark(
        metric_id="paid_orders",
        time_field="paid_at",
        sql=(
            "SELECT COUNT(DISTINCT order_no) AS paid_orders\n"
            "FROM fact_order_items\n"
            f"WHERE {_windowed('paid_at')}"
        ),
    ),
    "avg_order_value": Benchmark(
        metric_id="avg_order_value",
        time_field="paid_at",
        notes="分母用 NULLIF 防除零，空结果解释为零订单",
        sql=(
            "SELECT SUM(paid_amount_minor) / NULLIF(COUNT(DISTINCT order_no), 0) AS avg_order_value_minor\n"
            "FROM fact_order_items\n"
            f"WHERE {_windowed('paid_at')}"
        ),
    ),
    "refund_amount": Benchmark(
        metric_id="refund_amount",
        time_field="completed_at",
        notes="按退款完成时间归集，不与支付金额混算",
        sql=(
            "SELECT category, SUM(refund_amount_minor) AS refund_amount_minor\n"
            "FROM fact_refunds\n"
            f"WHERE {_windowed('completed_at')}\n"
            "GROUP BY category\n"
            "ORDER BY refund_amount_minor DESC\n"
            "LIMIT 10"
        ),
    ),
    "refund_units": Benchmark(
        metric_id="refund_units",
        time_field="completed_at",
        sql=(
            "SELECT category, SUM(refund_qty) AS refund_units\n"
            "FROM fact_refunds\n"
            f"WHERE {_windowed('completed_at')}\n"
            "GROUP BY category\n"
            "ORDER BY refund_units DESC\n"
            "LIMIT 10"
        ),
    ),
    "product_sales_rank": Benchmark(
        metric_id="product_sales_rank",
        time_field="paid_at",
        include_zero=True,
        notes="LEFT JOIN 商品维度，零销量商品以 COALESCE 保留并说明",
        sql=(
            "SELECT p.product_id, p.title, COALESCE(SUM(f.quantity), 0) AS paid_units\n"
            "FROM dim_product AS p\n"
            "LEFT JOIN fact_order_items AS f\n"
            "  ON f.product_id = p.product_id AND " + _windowed("f.paid_at") + "\n"
            "GROUP BY p.product_id, p.title\n"
            "ORDER BY paid_units DESC\n"
            "LIMIT 10"
        ),
    ),
    "category_sales_rank": Benchmark(
        metric_id="category_sales_rank",
        time_field="paid_at",
        include_zero=True,
        notes="已启用品类左连接聚合，零销量品类以 COALESCE 保留并说明",
        sql=(
            "SELECT c.category, COALESCE(SUM(f.quantity), 0) AS paid_units\n"
            "FROM (SELECT DISTINCT category FROM dim_product) AS c\n"
            "LEFT JOIN fact_order_items AS f\n"
            "  ON f.category = c.category AND " + _windowed("f.paid_at") + "\n"
            "GROUP BY c.category\n"
            "ORDER BY paid_units DESC\n"
            "LIMIT 10"
        ),
    ),
    "available_inventory": Benchmark(
        metric_id="available_inventory",
        time_field=None,
        notes="dim_sku_inventory 为最新快照，不对时间做窗口过滤",
        sql=(
            "SELECT category, SUM(available_qty) AS available_qty\n"
            "FROM dim_sku_inventory\n"
            "GROUP BY category\n"
            "ORDER BY available_qty DESC\n"
            "LIMIT 10"
        ),
    ),
    "low_stock_skus": Benchmark(
        metric_id="low_stock_skus",
        time_field=None,
        notes="阈值默认 10，需在结果中说明",
        sql=(
            "SELECT sku_id, title, available_qty\n"
            "FROM dim_sku_inventory\n"
            "WHERE available_qty < 10\n"
            "ORDER BY available_qty ASC\n"
            "LIMIT 10"
        ),
    ),
}


def benchmark_for(metric_id: str) -> Benchmark | None:
    """按指标 ID 返回人工基准。"""
    return BENCHMARKS.get(metric_id)