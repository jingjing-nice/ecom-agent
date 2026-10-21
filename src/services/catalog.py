"""商品事实查询：统一可售范围，在数据库中选取每个 SPU 的最低价 SKU。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import ActorScope
from src.models import InventorySnapshot, Product, ProductSKU


CARD_LIMIT = 3
# 图谱候选复核需要比展示卡片更长的核验名单
VERIFY_LIMIT = 10


def _sellable_query(actor: ActorScope) -> Select:
    return (
        select(Product, ProductSKU, InventorySnapshot)
        .join(ProductSKU, (ProductSKU.tenant_id == Product.tenant_id)
              & (ProductSKU.product_id == Product.product_id))
        .join(InventorySnapshot, (InventorySnapshot.tenant_id == ProductSKU.tenant_id)
              & (InventorySnapshot.sku_id == ProductSKU.sku_id))
        .where(
            Product.tenant_id == actor.tenant_id,
            Product.shop_id.in_(actor.shop_ids),
            ProductSKU.shop_id == Product.shop_id,
            InventorySnapshot.shop_id == Product.shop_id,
            Product.status == "on_sale",
            ProductSKU.status == "on_sale",
            InventorySnapshot.available_qty > 0,
        )
    )


def _apply_constraints(query: Select, *, category=None, budget_minor=None, brand=None,
                       size=None, exclude=None) -> Select:
    """所有召回路径在 SKU 排序和截断之前应用相同业务约束。"""
    if category:
        query = query.where(Product.category == category)
    if budget_minor is not None:
        query = query.where(ProductSKU.price_minor <= budget_minor)
    if brand:
        query = query.where(Product.brand == brand)
    if size:
        query = query.where(ProductSKU.specs["size"].astext == size)
    if exclude:
        query = query.where(Product.product_id != exclude)
    return query


async def has_available_variant(
    db: AsyncSession, actor: ActorScope, *, product_id: str, shop_id: str, size: str,
) -> bool:
    query = _sellable_query(actor).with_only_columns(ProductSKU.sku_id).where(
        Product.product_id == product_id,
        Product.shop_id == shop_id,
        ProductSKU.specs["size"].astext == size,
    ).limit(1)
    return await db.scalar(query) is not None


def _card(product: Product, sku: ProductSKU, stock: InventorySnapshot) -> dict[str, Any]:
    """卡片事实一律由已核验的数据库行结构化生成（方案 §4）。"""
    return {
        "product_id": product.product_id, "sku_id": sku.sku_id,
        "title": product.title, "brand": product.brand, "category": product.category,
        "specs": sku.specs, "price_minor": sku.price_minor, "currency": sku.currency,
        "available_qty": stock.available_qty, "observed_at": stock.observed_at.isoformat(),
        "url": f"/products/{product.product_id}", "source": "sandbox_catalog",
    }


async def _ranked_cards(db: AsyncSession, query: Select, *, limit: int = CARD_LIMIT) -> list[dict[str, Any]]:
    """先按 SPU 去重再 LIMIT；先截取 SKU 会让多规格商品挤掉其他候选。"""
    candidates = query.with_only_columns(
        Product.id.label("product_pk"),
        ProductSKU.id.label("sku_pk"),
        InventorySnapshot.id.label("stock_pk"),
        func.row_number().over(
            partition_by=Product.id,
            order_by=(ProductSKU.price_minor, ProductSKU.sku_id),
        ).label("product_rank"),
    ).subquery()
    ranked = (
        select(Product, ProductSKU, InventorySnapshot)
        .select_from(candidates)
        .join(Product, Product.id == candidates.c.product_pk)
        .join(ProductSKU, ProductSKU.id == candidates.c.sku_pk)
        .join(InventorySnapshot, InventorySnapshot.id == candidates.c.stock_pk)
        .where(candidates.c.product_rank == 1)
        .order_by(ProductSKU.price_minor, ProductSKU.sku_id)
        .limit(limit)
    )
    rows = (await db.execute(ranked)).all()
    return [_card(product, sku, stock) for product, sku, stock in rows]


async def sellable_facts(
    db: AsyncSession,
    actor: ActorScope,
    *,
    product_ids: list[str],
    size: str | None = None,
    category: str | None = None,
    budget_minor: int | None = None,
    brand: str | None = None,
    exclude: str | None = None,
    limit: int = VERIFY_LIMIT,
) -> list[dict[str, Any]]:
    """在授权范围内复核指定商品的可售事实（价格/库存/规格）。

    图谱候选与文本候选必须经此复核后才能进入卡片（方案 §13）。
    """
    if not product_ids:
        return []
    query = _sellable_query(actor).where(Product.product_id.in_(product_ids[:VERIFY_LIMIT]))
    query = _apply_constraints(query, category=category, budget_minor=budget_minor,
                               brand=brand, size=size, exclude=exclude)
    return await _ranked_cards(db, query, limit=limit)


async def search_products(
    db: AsyncSession,
    actor: ActorScope,
    *,
    query: str | None = None,
    category: str | None = None,
    budget_minor: int | None = None,
    brand: str | None = None,
    size: str | None = None,
    exclude: str | None = None,
) -> list[dict[str, Any]]:
    if query:
        from src.services.product_search import hybrid_product_search
        return await hybrid_product_search(db, actor, query=query, category=category,
            budget_minor=budget_minor, brand=brand, size=size, exclude=exclude)
    query = _apply_constraints(_sellable_query(actor), category=category,
                               budget_minor=budget_minor, brand=brand, size=size, exclude=exclude)
    return await _ranked_cards(db, query)



def merge_cards(*card_lists: list[dict[str, Any]], limit: int = CARD_LIMIT) -> list[dict[str, Any]]:
    """图谱与文本候选合并：按商品去重后统一排序（价格、SKU）再截断（方案 §13）。

    去重保留先出现的候选（文本候选已按价格排序）；统一排序保证图谱
    补充的低价商品不会被追加在文本候选之后，两路候选口径一致。
    卡片契约必含 product_id（去重与跳转依据），缺失标识的候选不进入卡片。
    """
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cards in card_lists:
        for card in cards:
            product_id = card.get("product_id")
            if not product_id or product_id in seen:
                continue
            seen.add(product_id)
            merged.append(card)
    merged.sort(key=lambda card: (card.get("price_minor", 0), card.get("sku_id") or ""))
    return merged[:limit]
