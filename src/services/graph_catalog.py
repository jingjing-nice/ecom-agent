"""图谱候选与实时事实复核（方案 §13）。

职责：
- 图谱只提供关系依据，不承担实时价格和库存的事实存储。
- 服务端只调用四类参数化模板（attributes / complements / alternatives / brand_relations）。
- 图谱候选必须回业务库复核可售事实后才能进入商品卡片；未复核项只作为线索不当作在售。
"""
from __future__ import annotations

from src.core.deps import ActorScope
from src.core.exceptions import AppError
from src.graph import (
    brand_relations,
    find_alternatives,
    find_by_attributes,
    find_complements,
)
from src.services.catalog import sellable_facts

# 服务端白名单：模型只能选择关系类型，不能生成任意 Cypher
GRAPH_KINDS = ("attributes", "complements", "alternatives", "brand_relations")
# 单次复核的商品上限，防止一次请求放大成全表查询
CANDIDATE_LIMIT = 10


async def graph_candidates(
    db,
    actor: ActorScope,
    *,
    kind: str,
    category: str | None = None,
    brand: str | None = None,
    scene: str | None = None,
    budget_minor: int | None = None,
    size: str | None = None,
    exclude: str | None = None,
    product_ids: list[str] | None = None,
) -> dict:
    """查询图谱关系并复核候选商品的可售事实。

    返回 relations（关系依据）与 verified_products（已复核可售事实），
    并逐条标注 verified，供上层区分「关系存在」与「当前可售」。
    """
    if kind not in GRAPH_KINDS:
        raise AppError(f"不支持的图谱关系类型：{kind}")
    ids = [pid for pid in (product_ids or []) if pid][:CANDIDATE_LIMIT]

    if kind == "attributes":
        relations = await find_by_attributes(
            tenant_id=actor.tenant_id, category=category, brand=brand, scene=scene,
            limit=CANDIDATE_LIMIT,
        )
    elif kind == "complements":
        if not ids:
            raise AppError("查询搭配关系需要先确定参考商品")
        relations = await find_complements(tenant_id=actor.tenant_id, product_ids=ids, limit=CANDIDATE_LIMIT)
    elif kind == "alternatives":
        if not ids:
            raise AppError("查询替代关系需要先确定参考商品")
        relations = await find_alternatives(tenant_id=actor.tenant_id, product_ids=ids, limit=CANDIDATE_LIMIT)
    else:
        relations = await brand_relations(tenant_id=actor.tenant_id, brand=brand or "", limit=CANDIDATE_LIMIT)

    relation_ids = [
        row["product_id"] for row in relations if row.get("product_id")
    ][:CANDIDATE_LIMIT]
    verified = await sellable_facts(
        db, actor, product_ids=relation_ids, budget_minor=budget_minor, size=size,
        category=category if kind != "complements" else None,
        brand=brand, exclude=exclude,
    ) if relation_ids else []
    verified_ids = {card["product_id"] for card in verified}

    for row in relations:
        if row.get("product_id"):
            row["verified"] = row["product_id"] in verified_ids
    return {
        "kind": kind,
        "relations": relations,
        "verified_products": verified,
        "unverified_count": sum(1 for row in relations if row.get("product_id") and not row["verified"]),
        "note": "图谱仅解释关系依据；价格与库存以复核后的可售事实为准。",
    }
