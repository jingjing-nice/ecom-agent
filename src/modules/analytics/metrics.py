"""指标语义层（方案 §6）：10 个首批指标的权威口径定义。

口径约定：
- 时间统一 UTC 保存；查询以 Asia/Shanghai 解释自然语言时间后转 UTC 半开区间 [start, end)
- 金额为最小货币单位整数
- 品类归属采用成交快照；最低销量排名默认包含已启用品类的零销量项并在结果中说明
- 净收入等跨期指标未开放（须业务负责人另行定义）
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MetricDefinition:
    """单个指标的口径定义。"""

    metric_id: str
    name: str
    definition: str
    # 归集时间字段（用于校验查询时间字段与口径版本）
    time_field: str
    allowed_dimensions: list[str] = field(default_factory=list)
    default_sort: str = "desc"
    # 口径版本，变更时递增并在评测报告中说明
    version: str = "v1"
    notes: str = ""


# 允许的分析维度（对应分析库列）
DIMENSIONS = {
    "category": "品类（成交快照）",
    "brand": "品牌（成交快照）",
    "shop_id": "店铺",
    "region": "买家地区（画像分组）",
    "age_group": "买家年龄段（画像分组）",
    "date": "日期（Asia/Shanghai 自然日）",
}

METRICS: dict[str, MetricDefinition] = {
    "paid_amount": MetricDefinition(
        metric_id="paid_amount",
        name="支付金额",
        definition="时间窗口内支付成功订单明细的实际支付金额之和（最小货币单位）",
        time_field="paid_at",
        allowed_dimensions=["category", "brand", "shop_id", "date", "region", "age_group"],
    ),
    "paid_units": MetricDefinition(
        metric_id="paid_units",
        name="支付件数",
        definition="时间窗口内支付成功订单的明细 quantity 之和",
        time_field="paid_at",
        allowed_dimensions=["category", "brand", "shop_id", "date"],
    ),
    "paid_orders": MetricDefinition(
        metric_id="paid_orders",
        name="支付订单数",
        definition="时间窗口内支付成功订单的去重订单数",
        time_field="paid_at",
        allowed_dimensions=["shop_id", "date"],
    ),
    "avg_order_value": MetricDefinition(
        metric_id="avg_order_value",
        name="客单价",
        definition="支付金额 ÷ 支付订单数；分母为零时返回空值并解释",
        time_field="paid_at",
        allowed_dimensions=["shop_id", "category", "date"],
    ),
    "refund_amount": MetricDefinition(
        metric_id="refund_amount",
        name="退款完成金额",
        definition="按退款完成时间归集的退款金额之和（单独统计，不与支付金额混算）",
        time_field="completed_at",
        allowed_dimensions=["category", "shop_id", "date"],
    ),
    "refund_units": MetricDefinition(
        metric_id="refund_units",
        name="退款完成件数",
        definition="按退款完成时间归集的退款数量之和",
        time_field="completed_at",
        allowed_dimensions=["category", "shop_id", "date"],
    ),
    "product_sales_rank": MetricDefinition(
        metric_id="product_sales_rank",
        name="商品销量排名",
        definition="按支付件数降序的商品排名；最低销量排名默认包含零销量项并说明",
        time_field="paid_at",
        allowed_dimensions=["category", "brand", "shop_id"],
        default_sort="desc",
        notes="include_zero=true 时对授权品类做左连接，包含零销量商品",
    ),
    "category_sales_rank": MetricDefinition(
        metric_id="category_sales_rank",
        name="品类销量排名",
        definition="按支付件数降序的品类排名；默认包含已启用但零销量的品类",
        time_field="paid_at",
        allowed_dimensions=["shop_id"],
        default_sort="desc",
        notes="include_zero=true 时包含零销量品类",
    ),
    "available_inventory": MetricDefinition(
        metric_id="available_inventory",
        name="可售库存",
        definition="当前可售 SKU 的可用库存数量之和（最新快照）",
        time_field="observed_at",
        allowed_dimensions=["category", "brand", "shop_id"],
    ),
    "low_stock_skus": MetricDefinition(
        metric_id="low_stock_skus",
        name="低库存 SKU",
        definition="可用库存低于阈值（默认 10）的可售 SKU 列表",
        time_field="observed_at",
        allowed_dimensions=["category", "brand", "shop_id"],
        default_sort="asc",
        notes="阈值为参数，默认 10，需在结果中说明",
    ),
}

# 分析库授权数据集（NL2SQL 只允许访问这些表）
ANALYTICS_DATASETS: dict[str, dict] = {
    "fact_order_items": {
        "description": "支付成功订单明细事实表（脱敏，买家标识为匿名哈希）",
        "columns": {
            "order_no": "订单号（匿名化后）",
            "shop_id": "店铺 ID",
            "category": "品类（成交快照）",
            "brand": "品牌（成交快照）",
            "product_id": "商品 ID",
            "sku_id": "SKU ID",
            "quantity": "数量",
            "paid_amount_minor": "实付金额（分）",
            "paid_at": "支付时间（UTC）",
            "region": "买家地区分组",
            "age_group": "买家年龄段分组",
        },
    },
    "fact_refunds": {
        "description": "退款完成事实表",
        "columns": {
            "order_no": "订单号（匿名化后）",
            "shop_id": "店铺 ID",
            "category": "品类（成交快照）",
            "refund_qty": "退款数量",
            "refund_amount_minor": "退款金额（分）",
            "completed_at": "退款完成时间（UTC）",
        },
    },
    "dim_sku_inventory": {
        "description": "SKU 库存维度-事实表（最新快照）",
        "columns": {
            "sku_id": "SKU ID",
            "product_id": "商品 ID",
            "title": "商品标题",
            "category": "品类",
            "brand": "品牌",
            "shop_id": "店铺 ID",
            "available_qty": "可用库存",
            "observed_at": "观测时间（UTC）",
        },
    },
    "dim_product": {
        "description": "商品维度表",
        "columns": {
            "product_id": "商品 ID",
            "title": "商品标题",
            "category": "品类",
            "brand": "品牌",
            "shop_id": "店铺 ID",
        },
    },
}

ALLOWED_TABLES = set(ANALYTICS_DATASETS.keys())
ALLOWED_COLUMNS: dict[str, set[str]] = {
    table: set(info["columns"].keys()) for table, info in ANALYTICS_DATASETS.items()
}


def get_metric_schema(metric_id: str | None = None) -> dict | list[dict]:
    """返回指标定义（供运营 Agent 的 get_metric_schema 工具使用）。"""
    if metric_id:
        metric = METRICS.get(metric_id)
        if metric is None:
            return []
        return [metric.__dict__]
    return [m.__dict__ for m in METRICS.values()]