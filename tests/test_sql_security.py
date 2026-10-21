import pytest
from src.core.deps import ActorScope
from src.core.exceptions import PermissionDenied
from src.nl2sql.validator import validate_sql
from src.nl2sql.executor import scoped_sql


@pytest.mark.parametrize("sql", [
    "DELETE FROM fact_order_items", "SELECT pg_sleep(1)",
    "SELECT * FROM pg_catalog.pg_user", "SELECT * FROM other.fact_order_items",
    "SELECT * FROM fact_order_items; DROP TABLE users",
    "WITH bad AS (DELETE FROM fact_order_items RETURNING *) SELECT * FROM bad",
    "SELECT unknown_function(quantity) FROM fact_order_items",
    "SELECT missing_field FROM fact_order_items",
    "SELECT (SELECT quantity INTO stolen FROM fact_order_items LIMIT 1)",
    "SELECT * FROM fact_order_items FOR UPDATE",
    # 布尔连接词中的危险函数仍须拒绝
    "SELECT SUM(quantity) FROM fact_order_items WHERE pg_sleep(1) IS NOT NULL AND quantity > 0",
    "SELECT SUM(quantity) FROM fact_order_items WHERE dblink('x') IS NOT NULL OR quantity > 0",
])
def test_rejects_unsafe_sql(sql):
    assert not validate_sql(sql).ok


@pytest.mark.parametrize("sql", [
    "SELECT SUM(quantity) AS units FROM fact_order_items",
    "WITH sales AS (SELECT category, SUM(quantity) AS units FROM fact_order_items GROUP BY category) SELECT * FROM sales",
    "SELECT product_id FROM dim_product UNION SELECT product_id FROM dim_sku_inventory",
    # 布尔连接词是结构谓词：带 WHERE 多条件的合法查询不能被误判为危险函数
    "SELECT SUM(quantity) AS units FROM fact_order_items WHERE paid_at >= '2026-10-01 00:00:00+00' AND paid_at < '2026-10-18 00:00:00+00'",
    "SELECT category, SUM(quantity) FROM fact_order_items WHERE (paid_at >= '2026-10-01' OR quantity > 0) GROUP BY category",
])
def test_allows_scoped_aggregates(sql):
    result = validate_sql(sql)
    assert result.ok, result.reason


def test_limits_are_capped():
    assert "LIMIT 1000" in validate_sql("SELECT * FROM dim_product LIMIT 999999").normalized_sql


def test_injects_scope_into_every_physical_dataset():
    actor = ActorScope("user", "tenant-a", "ops", ["shop-a"])
    sql, params = scoped_sql("WITH x AS (SELECT product_id FROM dim_product) SELECT product_id FROM x UNION SELECT product_id FROM dim_sku_inventory", actor, 100)
    assert sql.count("tenant_id = :scope_tenant") == 2
    assert sql.count("shop_id IN") == 2
    assert params == {"scope_tenant": "tenant-a", "scope_shops": '["shop-a"]'}


def test_analysis_role_required():
    with pytest.raises(PermissionDenied):
        scoped_sql("SELECT * FROM dim_product", ActorScope("buyer", "demo", "buyer", ["shop-a"]), 100)


@pytest.mark.parametrize("sql", [
    "SELECT quantity, (SELECT SUM(quantity) FROM fact_order_items) AS total FROM fact_order_items",
    "SELECT SUM(quantity) OVER () FROM fact_order_items",
    "SELECT SUM(quantity) FROM fact_order_items UNION ALL SELECT quantity FROM fact_order_items",
    "WITH raw AS (SELECT quantity FROM fact_order_items) SELECT SUM(quantity) FROM raw",
    "SELECT DISTINCT order_no, SUM(quantity) FROM fact_order_items GROUP BY order_no",
    "SELECT * FROM dim_product a JOIN dim_product a ON a.product_id = a.product_id",
])
def test_rejects_scope_and_aggregation_bypasses(sql):
    assert not validate_sql(sql).ok


def test_parenthesized_query_is_still_capped():
    result = validate_sql("(SELECT product_id FROM dim_product LIMIT 999999)", max_rows=7)
    assert result.ok
    assert "LIMIT 7" in result.normalized_sql


@pytest.mark.parametrize("limit", [0, -1, True])
def test_rejects_invalid_row_caps(limit):
    assert not validate_sql("SELECT product_id FROM dim_product", max_rows=limit).ok


def test_tenant_binding_does_not_rewrite_string_literals():
    actor = ActorScope("user", "tenant-a", "ops", ["shop-a"])
    sql, _ = scoped_sql("SELECT '%(scope_tenant)s' AS marker, product_id FROM dim_product", actor, 10)
    assert "'%(scope_tenant)s'" in sql
    assert "tenant_id = :scope_tenant" in sql


def test_distinct_order_count_remains_allowed():
    result = validate_sql("SELECT COUNT(DISTINCT order_no) AS orders FROM fact_order_items")
    assert result.ok, result.reason
