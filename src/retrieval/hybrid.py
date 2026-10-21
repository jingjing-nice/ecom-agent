"""混合检索：关键词召回 + 向量召回 + RRF 融合（方案 §4）。

- 关键词召回：jieba 分词后的 OR 匹配，按命中词数排序
- 向量召回：pgvector 余弦距离（HNSW 索引）
- RRF 融合去重；向量服务失败时降级为纯关键词检索
- 服务层最终执行权限与事实核验
"""
from __future__ import annotations

from dataclasses import dataclass

import jieba
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings

# 关键词召回的停用词（轻量，仅过滤无检索意义的词）
_STOPWORDS = {
    "的", "了", "吗", "呢", "是", "在", "有", "和", "与", "或", "怎么", "如何",
    "可以", "能否", "请问", "一下", "这个", "那个", "什么", "哪些",
}


@dataclass
class HybridSearchHit:
    """融合后的检索命中。"""

    chunk_id: str
    policy_version_id: str
    doc_id: str
    doc_title: str
    version: str
    scope: str
    section_title: str
    content: str
    char_start: int
    char_end: int
    rrf_score: float = 0.0
    keyword_rank: int | None = None
    vector_rank: int | None = None
    distance: float | None = None


def tokenize(query: str, *, max_terms: int = 8) -> list[str]:
    """jieba 分词 + 停用词过滤。"""
    terms: list[str] = []
    for term in jieba.lcut(query):
        cleaned = term.strip()
        if not cleaned or cleaned in _STOPWORDS:
            continue
        if len(cleaned) == 1 and not cleaned.isdigit():
            # 单字（中文）噪音大，保留数字
            continue
        if cleaned not in terms:
            terms.append(cleaned)
        if len(terms) >= max_terms:
            break
    return terms


def rrf_fuse(ranked_lists: dict[str, list[str]], k: int = 60) -> dict[str, float]:
    """Reciprocal Rank Fusion：score = Σ 1/(k + rank)。"""
    scores: dict[str, float] = {}
    for ids in ranked_lists.values():
        for rank, item_id in enumerate(ids, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
    return scores


_JOIN_CLAUSE = """
FROM policy_chunks pc
JOIN policy_versions pv ON pv.id = pc.policy_version_id
JOIN policy_documents pd ON pd.id = pv.policy_document_id
"""

_FILTER_CLAUSE = """
WHERE pc.tenant_id = :tenant_id
  AND pv.tenant_id = :tenant_id
  AND pd.tenant_id = :tenant_id
  AND pv.status = 'published'
  AND (CAST(:scope AS text) IS NULL OR pv.scope = CAST(:scope AS text))
  AND pv.effective_from <= now()
  AND (pv.effective_to IS NULL OR pv.effective_to > now())
"""

_SELECT_COLUMNS = """
SELECT pc.id::text AS chunk_id,
       pc.policy_version_id::text AS policy_version_id,
       pc.section_title, pc.content, pc.char_start, pc.char_end,
       pv.version, pv.scope,
       pd.doc_id, pd.title AS doc_title
"""


async def _keyword_search(
    db: AsyncSession,
    query: str,
    *,
    tenant_id: str,
    scope: str | None,
    limit: int,
) -> list[dict]:
    """关键词召回：OR 匹配 + 命中词数排序。"""
    terms = tokenize(query)
    if not terms:
        return []

    or_parts = []
    score_parts = []
    params: dict = {"tenant_id": tenant_id, "scope": scope, "limit": limit}
    for i, term in enumerate(terms):
        key = f"kw{i}"
        params[key] = f"%{term}%"
        or_parts.append(f"pc.content ILIKE :{key} OR pc.section_title ILIKE :{key}")
        score_parts.append(f"CASE WHEN pc.content ILIKE :{key} THEN 1 ELSE 0 END")

    sql = (
        _SELECT_COLUMNS
        + f", ({' + '.join(score_parts)}) AS hit_count\n"
        + _JOIN_CLAUSE
        + _FILTER_CLAUSE
        + f" AND ({' OR '.join(or_parts)})\n"
        + "ORDER BY hit_count DESC, pc.chunk_index ASC\nLIMIT :limit"
    )
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [dict(row) for row in rows]


async def _vector_search(
    db: AsyncSession,
    query: str,
    *,
    tenant_id: str,
    scope: str | None,
    limit: int,
) -> list[dict]:
    """向量召回：pgvector 余弦距离。Embedding 失败时降级为空结果。"""
    try:
        if not get_settings().embedding_api_key:
            return []
        from src.retrieval.rag import GatewayEmbeddings
        vector = await GatewayEmbeddings().aembed_query(query)
        if len(vector) != get_settings().EMBEDDING_DIM:
            raise ValueError("Embedding 维度与索引不一致")
        vec_literal = "[" + ",".join(f"{x:.6f}" for x in vector) + "]"
    except Exception as exc:
        logger.warning(f"向量召回降级（Embedding 失败）：{exc}")
        return []

    sql = (
        _SELECT_COLUMNS
        + ", pc.embedding <=> CAST(:vec AS vector) AS distance\n"
        + _JOIN_CLAUSE
        + _FILTER_CLAUSE
        + " AND pc.embedding IS NOT NULL AND pc.model_version = :model_version AND pv.index_version = :model_version\n"
        + "ORDER BY pc.embedding <=> CAST(:vec AS vector)\nLIMIT :limit"
    )
    params = {
        "tenant_id": tenant_id,
        "scope": scope,
        "limit": limit,
        "vec": vec_literal,
        "model_version": get_settings().EMBEDDING_MODEL,
    }
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [dict(row) for row in rows]


async def hybrid_policy_search(
    db: AsyncSession,
    query: str,
    *,
    tenant_id: str,
    scope: str | None = None,
    top_k: int | None = None,
) -> list[HybridSearchHit]:
    """兼容返回契约；实际由 LangChain Retriever / 串行 RAG Runnable 执行。"""
    from src.retrieval.rag import build_policy_rag
    state = await build_policy_rag(db, tenant_id=tenant_id, scope=scope, top_k=top_k).ainvoke({"query": query})
    return state["hits"]


def detect_version_conflict(hits: list[HybridSearchHit]) -> list[dict]:
    """检测同一主题（文档）多个已发布版本同时命中的口径冲突。

    命中冲突时上层停止给确定结论，进入运营修订（方案 §4）。
    """
    by_doc: dict[str, set[str]] = {}
    for hit in hits:
        by_doc.setdefault(hit.doc_id, set()).add(hit.version)
    return [
        {"doc_id": doc_id, "versions": sorted(versions)}
        for doc_id, versions in by_doc.items()
        if len(versions) > 1
    ]
