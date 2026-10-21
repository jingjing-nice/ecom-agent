"""商品知识图谱参数化查询模板（方案 §13）。

服务端只开放四类模板：find_by_attributes / find_complements /
find_alternatives / brand_relations。最多 2 跳、结果有上限、服务端超时；
不允许模型生成任意 Cypher 或开放 procedure。路径各节点与结果再次核验租户。

图谱只解释关系依据，不承担实时价格和库存的事实存储。
"""
from __future__ import annotations

from typing import Any

from loguru import logger
from neo4j import Query

from src.infra.neo4j_client import get_neo4j_driver

GRAPH_QUERY_TIMEOUT_SECONDS = 5.0
GRAPH_MAX_LIMIT = 20

# ------------------------------------------------------------------
# 模板 1：按结构化条件筛选商品（品类/品牌/场景）
# ------------------------------------------------------------------
_FIND_BY_ATTRIBUTES = """
MATCH (p:Product)
WHERE p.tenant_id = $tenant_id
  AND p.status = 'on_sale'
  AND ($category IS NULL OR p.category = $category)
  AND ($brand IS NULL OR p.brand = $brand)
OPTIONAL MATCH (p)-[r:FIT_SCENE]->(s:Scene)
WHERE s.tenant_id = $tenant_id AND r.status = 'approved'
WITH p, collect(DISTINCT s.name) AS scenes
WHERE $scene IS NULL OR $scene IN scenes
RETURN p.biz_id AS product_id, p.title AS title, p.category AS category,
       p.brand AS brand, scenes
LIMIT $limit
"""

# ------------------------------------------------------------------
# 模板 2：搭配关系（GOES_WITH，双向 1 跳，仅 approved 关系）
# ------------------------------------------------------------------
_FIND_COMPLEMENTS = """
MATCH (p:Product)-[r:GOES_WITH]-(q:Product)
WHERE p.tenant_id = $tenant_id
  AND q.tenant_id = $tenant_id
  AND p.biz_id IN $product_ids
  AND q.biz_id <> p.biz_id
  AND r.status = 'approved'
  AND (r.valid_from IS NULL OR datetime(r.valid_from) <= datetime())
  AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime())
RETURN DISTINCT q.biz_id AS product_id, q.title AS title, q.category AS category,
       q.brand AS brand, r.source_id AS source_id, r.source AS source,
       r.reason AS reason
LIMIT $limit
"""

# ------------------------------------------------------------------
# 模板 3：替代关系（ALTERNATIVE_TO，1 跳）
# ------------------------------------------------------------------
_FIND_ALTERNATIVES = """
MATCH (p:Product)-[r:ALTERNATIVE_TO]-(q:Product)
WHERE p.tenant_id = $tenant_id
  AND q.tenant_id = $tenant_id
  AND p.biz_id IN $product_ids
  AND q.biz_id <> p.biz_id
  AND r.status = 'approved'
  AND (r.valid_from IS NULL OR datetime(r.valid_from) <= datetime())
  AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime())
RETURN DISTINCT q.biz_id AS product_id, q.title AS title, q.category AS category,
       q.brand AS brand, r.source_id AS source_id, r.source AS source,
       r.reason AS reason
LIMIT $limit
"""

# ------------------------------------------------------------------
# 模板 4：品牌关系（SUB_BRAND_OF / 同品牌商品，1 跳）
# ------------------------------------------------------------------
_BRAND_RELATIONS = """
MATCH (b1:Brand)-[r:SUB_BRAND_OF]-(b2:Brand)
WHERE b1.tenant_id = $tenant_id AND b2.tenant_id = $tenant_id
  AND b1.name = $brand
  AND r.status = 'approved'
  AND (r.valid_from IS NULL OR datetime(r.valid_from) <= datetime())
  AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime())
RETURN DISTINCT b2.name AS related_brand, r.relation_type AS relation_type,
       r.source AS source, r.source_id AS source_id
LIMIT $limit
"""


async def _run(query: str, params: dict[str, Any]) -> list[dict]:
    """执行模板查询（服务端超时 + 异常降级为空结果）。"""
    driver = get_neo4j_driver()
    try:
        records, _, _ = await driver.execute_query(
            Query(query, timeout=GRAPH_QUERY_TIMEOUT_SECONDS),
            params,
            database_="neo4j",
        )
        return [dict(record) for record in records]
    except Exception as exc:
        logger.warning(f"图谱查询失败（降级为空结果）：{exc}")
        return []


def _clean_limit(limit: int | None) -> int:
    if not limit or limit <= 0:
        return 10
    return min(limit, GRAPH_MAX_LIMIT)


async def find_by_attributes(
    *,
    tenant_id: str,
    category: str | None = None,
    brand: str | None = None,
    scene: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """按品类/品牌/场景筛选商品。"""
    return await _run(
        _FIND_BY_ATTRIBUTES,
        {
            "tenant_id": tenant_id,
            "category": category,
            "brand": brand,
            "scene": scene,
            "limit": _clean_limit(limit),
        },
    )


async def find_complements(
    *, tenant_id: str, product_ids: list[str], limit: int = 10
) -> list[dict]:
    """查询搭配商品（人工核验通过的 GOES_WITH 关系）。"""
    if not product_ids:
        return []
    return await _run(
        _FIND_COMPLEMENTS,
        {
            "tenant_id": tenant_id,
            "product_ids": product_ids[:10],
            "limit": _clean_limit(limit),
        },
    )


async def find_alternatives(
    *, tenant_id: str, product_ids: list[str], limit: int = 10
) -> list[dict]:
    """查询替代商品（如缺货时的同品牌替代款）。"""
    if not product_ids:
        return []
    return await _run(
        _FIND_ALTERNATIVES,
        {
            "tenant_id": tenant_id,
            "product_ids": product_ids[:10],
            "limit": _clean_limit(limit),
        },
    )


async def brand_relations(
    *, tenant_id: str, brand: str, limit: int = 10
) -> list[dict]:
    """查询品牌关联（子品牌/集团归属，仅来自有来源的资料）。"""
    if not brand:
        return []
    return await _run(
        _BRAND_RELATIONS,
        {"tenant_id": tenant_id, "brand": brand, "limit": _clean_limit(limit)},
    )
