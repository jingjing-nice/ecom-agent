"""商品关键词、向量候选融合；最终复用商品服务核验所有硬约束。

图谱候选由 graph_lookup 提供并再次核验。无向量配置时可用关键词及结构化基线。
"""
from __future__ import annotations

import math
from sqlalchemy import case, or_, select
from loguru import logger

from src.core.config import get_settings
from src.infra.model_gateway import get_model_gateway
from src.models import Product, ProductSearchIndex
from src.retrieval.hybrid import rrf_fuse, tokenize

CANDIDATES = 30


def product_text(product) -> str:
    import json
    return "\n".join([product.title, product.brand, product.category, product.sub_category or "",
                      product.description or "", json.dumps(product.attributes or {}, ensure_ascii=False, sort_keys=True)])


def content_hash(product) -> str:
    import hashlib
    return hashlib.sha256(product_text(product).encode()).hexdigest()


def valid_vectors(vectors, count, dimension):
    if len(vectors) != count or any(len(v) != dimension or any(not math.isfinite(x) for x in v) for v in vectors):
        raise ValueError("Embedding 数量、维度或数值与索引契约不一致")


async def hybrid_product_search(db, actor, *, query, category=None, budget_minor=None,
                                brand=None, size=None, exclude=None):
    from src.services.catalog import _apply_constraints, _ranked_cards, _sellable_query, CARD_LIMIT
    settings = get_settings()
    base = _apply_constraints(_sellable_query(actor), category=category, budget_minor=budget_minor,
                              brand=brand, size=size, exclude=exclude)
    terms = tokenize(query)
    matches = [or_(Product.title.icontains(t, autoescape=True), Product.brand.icontains(t, autoescape=True),
                   Product.category.icontains(t, autoescape=True), Product.description.icontains(t, autoescape=True)) for t in terms]
    keyword_ids = []
    if matches:
        score = sum(case((m, 1), else_=0) for m in matches)
        stmt = base.with_only_columns(Product.product_id, score.label("score")).where(or_(*matches))
        rows = (await db.execute(stmt.distinct().order_by(score.desc(), Product.product_id).limit(CANDIDATES))).all()
        keyword_ids = [row[0] for row in rows]
    vector_ids = []
    if settings.embedding_api_key:
        try:
            vectors = await get_model_gateway().embed([query], purpose="product_retrieval")
            valid_vectors(vectors, 1, settings.EMBEDDING_DIM)
        except Exception:
            logger.warning("商品向量召回不可用，降级为关键词与结构化筛选")
        else:
            distance = ProductSearchIndex.embedding.cosine_distance(vectors[0])
            stmt = base.join(ProductSearchIndex,
                (ProductSearchIndex.tenant_id == Product.tenant_id)
                & (ProductSearchIndex.product_id == Product.product_id)).where(
                    ProductSearchIndex.model_version == settings.EMBEDDING_MODEL,
                    ProductSearchIndex.embedding.is_not(None),
                    ProductSearchIndex.source_updated_at == Product.updated_at,
                ).with_only_columns(Product.product_id, distance.label("distance"))
            rows = (await db.execute(stmt.distinct().order_by(distance, Product.product_id).limit(CANDIDATES))).all()
            vector_ids = [row[0] for row in rows]
    scores = rrf_fuse({"keyword": keyword_ids, "vector": vector_ids}, k=settings.RETRIEVAL_RRF_K)
    if not scores:
        return await _ranked_cards(db, base)
    ids = sorted(scores, key=lambda pid: (-scores[pid], pid))
    # 批量复核在同一授权和硬约束查询上完成，不信任索引中的价格或库存。
    cards = await _ranked_cards(db, base.where(Product.product_id.in_(ids)), limit=CANDIDATES)
    cards.sort(key=lambda c: (-scores[c["product_id"]], c["price_minor"], c["sku_id"]))
    for card in cards:
        pid = card["product_id"]
        card["retrieval"] = {"rrf_score": scores[pid], "sources": [name for name, ids_ in
            (("keyword", keyword_ids), ("vector", vector_ids)) if pid in ids_]}
    return cards[:CARD_LIMIT]
