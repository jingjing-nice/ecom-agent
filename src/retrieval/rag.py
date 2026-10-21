"""LangChain Retriever 接口 + 串行政策 RAG Runnable；数据库过滤始终在服务端注入。

answer_model 契约：async (messages, schema) -> AIMessage，其中 response_metadata["parsed"]
为 schema 实例（网关 structured 链路统一提供）。
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal, TypedDict

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.messages import HumanMessage
from langchain_core.retrievers import BaseRetriever
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel, Field

from src.core.config import get_settings
from src.core.exceptions import WorkflowError


class GatewayEmbeddings(Embeddings):
    """LangChain Embeddings 原生接口，统一沿用网关鉴权、维度校验和计量。"""
    def embed_documents(self, texts):
        raise NotImplementedError("仅支持异步 Embedding")

    def embed_query(self, text):
        raise NotImplementedError("仅支持异步 Embedding")

    async def aembed_documents(self, texts):
        from src.infra.model_gateway import get_model_gateway
        return await get_model_gateway().embed(texts, purpose="rag.index")

    async def aembed_query(self, text):
        from src.infra.model_gateway import get_model_gateway
        return (await get_model_gateway().embed([text], purpose="rag.query"))[0]


class PolicyRetriever(BaseRetriever):
    db: Any = Field(exclude=True)
    tenant_id: str
    scope: str | None = None
    mode: Literal["keyword", "vector"]
    k: int = Field(default=20, ge=1)

    def _get_relevant_documents(self, query, *, run_manager):
        raise NotImplementedError("政策检索仅支持异步调用")

    async def _aget_relevant_documents(self, query, *, run_manager):
        from src.retrieval.hybrid import _keyword_search, _vector_search
        search = _keyword_search if self.mode == "keyword" else _vector_search
        rows = await search(self.db, query, tenant_id=self.tenant_id, scope=self.scope, limit=self.k)
        return [Document(id=row["chunk_id"], page_content=row["content"],
            metadata={key: value for key, value in row.items() if key != "content"}) for row in rows]


class RAGState(TypedDict, total=False):
    query: str
    keyword: list[Document]
    vector: list[Document]
    hits: list[Any]
    conflicts: list[dict]
    excerpts: list[dict]
    answer: str


class Excerpt(BaseModel):
    chunk_id: str
    quote: str = Field(min_length=1)


class GroundedAnswer(BaseModel):
    excerpts: list[Excerpt] = Field(default_factory=list, max_length=3)


def build_policy_rag(db, *, tenant_id, scope=None, top_k=None, answer_model=None):
    from src.retrieval.hybrid import HybridSearchHit, detect_version_conflict, rrf_fuse
    settings = get_settings()
    k = settings.RETRIEVAL_TOP_K if top_k is None else top_k
    if isinstance(k, bool) or not isinstance(k, int) or k < 1 or k > 100:
        raise WorkflowError("检索数量必须为 1 至 100")
    limit = max(k*4,20)
    keyword = PolicyRetriever(db=db,tenant_id=tenant_id,scope=scope,mode="keyword",k=limit)
    vector = PolicyRetriever(db=db,tenant_id=tenant_id,scope=scope,mode="vector",k=limit)

    async def retrieve_keyword(state):
        return {"keyword": await keyword.ainvoke(state["query"])}

    async def retrieve_vector(state):
        # 同一 AsyncSession 不并发执行 SQL。
        return {"vector": await vector.ainvoke(state["query"])}

    def fuse(state):
        ids = {name:[doc.id for doc in state[name]] for name in ("keyword","vector")}
        scores = rrf_fuse(ids,k=settings.RETRIEVAL_RRF_K)
        docs = {doc.id:doc for doc in [*state["vector"],*state["keyword"]]}
        ranks = {name:{cid:i for i,cid in enumerate(values,1)} for name,values in ids.items()}
        # 冲突在截断之前检查，不能因为 Top-K 隐去另一个版本而继续给确定结论。
        hits = []
        for cid in sorted(scores,key=lambda cid:(-scores[cid],cid)):
            doc = docs[cid]
            meta = doc.metadata
            hits.append(HybridSearchHit(chunk_id=cid, policy_version_id=meta["policy_version_id"],
                doc_id=meta["doc_id"],doc_title=meta["doc_title"],version=meta["version"],scope=meta["scope"],
                section_title=meta.get("section_title") or "", content=doc.page_content,
                char_start=meta.get("char_start") or 0,char_end=meta.get("char_end") or 0,
                rrf_score=round(scores[cid],6),keyword_rank=ranks["keyword"].get(cid),
                vector_rank=ranks["vector"].get(cid),distance=meta.get("distance")))
        return {"hits": hits[:k], "conflicts":detect_version_conflict(hits)}

    async def generate(state):
        if not state["hits"] or state["conflicts"] or answer_model is None:
            return {"answer":"", "excerpts":[]}
        import json
        # 输出格式由 GroundedAnswer schema 约制（网关 structured 链路）；引句与原文精确比对仍在服务端执行。
        response = await answer_model([
            HumanMessage(content=json.dumps({"question":state["query"],"evidence":[asdict(h) for h in state["hits"]]},ensure_ascii=False))
        ], GroundedAnswer)
        parsed = response.response_metadata.get("parsed")
        if parsed is None:
            # 网关契约：structured() 成功必带 parsed；缺位说明返回不可信，中断而非降级。
            raise WorkflowError("回答模型未返回符合 schema 的结构化结果")
        by_id = {hit.chunk_id:hit for hit in state["hits"]}
        excerpts = []
        for excerpt in parsed.excerpts:
            hit = by_id.get(excerpt.chunk_id)
            if hit is None or excerpt.quote not in hit.content:
                raise WorkflowError("RAG 答复引用不匹配原文")
            excerpts.append({**excerpt.model_dump(),"version":hit.version,"section_title":hit.section_title})
        answer = "；".join(f"{item['quote']}（{item['version']}，{item['section_title']}）" for item in excerpts)
        return {"answer":answer,"excerpts":excerpts}

    async def pipeline(state):
        # 同一 AsyncSession 必须串行检索；固定流水线无需编译独立状态图。
        state = dict(state)
        state.update(await retrieve_keyword(state))
        state.update(await retrieve_vector(state))
        state.update(fuse(state))
        state.update(await generate(state))
        return state

    return RunnableLambda(pipeline, name="policy_rag")


async def retrieve_policy(db, query, *, tenant_id, scope="after_sale", answer_model=None):
    state = await build_policy_rag(db,tenant_id=tenant_id,scope=scope,answer_model=answer_model).ainvoke({"query":query})
    supported = state["hits"] if answer_model is None or state["excerpts"] or state["conflicts"] else []
    return {"evidence":[asdict(hit) for hit in supported],"conflicts":state["conflicts"],
            "answer":state["answer"],"excerpts":state["excerpts"]}
