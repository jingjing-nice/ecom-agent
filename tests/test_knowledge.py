"""知识文本导入、审核发布与无依据拒答（方案 §4）。

覆盖：
- split_text：空行分节、超长段落按句切分/硬切、原文定位与内容哈希
- import_document：draft 版本 + 片段 + 异步索引任务；同版本号冲突
- publish_blockers：审核状态、索引完成与 keyword-only 降级
- publish/revoke 路由：重叠版本拒绝、若有索引即可发布、撤销留痕
- 售后检索无依据/版本冲突时拒绝给出结论
"""
import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from pgvector.sqlalchemy import Vector

from src.api.knowledge import publish, revoke
from src.agents.subagents import validated_result
from src.agents.tools import AgentRuntime
from src.api.schemas import Chat, KnowledgeImport
from src.core.config import Settings
from src.core.deps import ActorScope
from src.core.exceptions import AppError, Conflict
from src.models import AuditLog, Job, PolicyChunk, PolicyDocument, PolicyVersion
from src.services.knowledge import import_document, publish_blockers, split_text


@compiles(JSONB, "sqlite")
def sqlite_json_type(type_, compiler, **kwargs):
    return "JSON"


@compiles(Vector, "sqlite")
def sqlite_vector_type(type_, compiler, **kwargs):
    return "JSON"


ADMIN = ActorScope("admin-a", "demo", "knowledge_admin", [])
BUYER = ActorScope("buyer-a", "demo", "buyer", ["a"])


@pytest.fixture
def knowledge_db():
    engine = create_engine("sqlite://")
    for model in (PolicyDocument, PolicyVersion, PolicyChunk, Job, AuditLog):
        table = model.__table__
        # 列级 index=True 与显式组合索引同名（真实库由迁移脚本控制），SQLite 建表前剔除单列自动索引。
        grouped: dict[str, list] = {}
        for index in list(table.indexes):
            grouped.setdefault(index.name, []).append(index)
        for group in grouped.values():
            if len(group) > 1:
                for index in group:
                    if len(index.columns) == 1:
                        table.indexes.discard(index)
        table.create(engine)
    with Session(engine) as session:
        db = SimpleNamespace(
            add=session.add,
            execute=AsyncMock(side_effect=session.execute),
            scalar=AsyncMock(side_effect=session.scalar),
            scalars=AsyncMock(side_effect=session.scalars),
            flush=AsyncMock(side_effect=session.flush),
            commit=AsyncMock(side_effect=session.commit),
        )
        yield session, db
    engine.dispose()


def payload(version="v2", content="退货须在签收后 7 天内提交。\n\n换货需保持商品完好。"):
    return KnowledgeImport(doc_id="refund-policy", title="退货换货政策", scope="after_sale",
                           version=version, content=content)


def test_split_text_keeps_offsets_titles_and_hashes():
    content = "退货须在签收后 7 天内提交。\n\n换货需保持商品完好。"
    chunks = split_text(content)
    assert [chunk["chunk_index"] for chunk in chunks] == [0]
    assert chunks[0]["content"] == content
    assert chunks[0]["char_start"] == 0 and chunks[0]["char_end"] == len(content)
    assert chunks[0]["content_hash"] == hashlib.sha256(content.encode()).hexdigest()
    assert chunks[0]["section_title"] == "退货须在签收后 7 天内提交。"


def test_split_text_splits_long_paragraph_by_sentence_boundary():
    content = "".join(f"第{i}句。" for i in range(30))
    chunks = split_text(content, max_chars=50)
    assert "".join(chunk["content"] for chunk in chunks) == content  # 不丢字
    assert all(len(chunk["content"]) <= 50 for chunk in chunks)
    assert len(chunks) > 1 and chunks[1]["char_start"] == chunks[0]["char_end"]


def test_split_text_hard_cuts_when_no_sentence_boundary():
    content = "啊" * 120
    chunks = split_text(content, max_chars=50)
    assert [len(chunk["content"]) for chunk in chunks] == [50, 50, 20]
    assert "".join(chunk["content"] for chunk in chunks) == content


async def test_import_document_enqueues_model_chunking_without_blocking_request(knowledge_db):
    session, db = knowledge_db
    result = await import_document(db, ADMIN, payload(), "trace")
    assert result["status"] == "draft" and result["chunk_count"] == 0
    session.commit()
    version = session.query(PolicyVersion).one()
    assert version.status == "draft" and version.index_version is None
    assert version.checksum == hashlib.sha256(payload().content.encode()).hexdigest()
    assert session.query(PolicyChunk).count() == 0
    job = session.query(Job).filter_by(job_type="knowledge_index").one()
    assert job.payload["version_id"] == result["version_id"] and job.status == "pending"
    assert job.payload["content"] == payload().content
    assert job.payload["content_hash"] == version.checksum
    audit = session.query(AuditLog).filter_by(action="knowledge.imported").one()
    assert audit.detail["chunking"] == "pending_model"


async def test_import_duplicate_version_is_rejected(knowledge_db):
    session, db = knowledge_db
    await import_document(db, ADMIN, payload(), "trace")
    session.commit()
    with pytest.raises(Conflict, match="同一版本号"):
        await import_document(db, ADMIN, payload(), "trace")


async def test_import_rejects_invalid_effective_range(knowledge_db):
    _, db = knowledge_db
    later = datetime(2026, 11, 1, tzinfo=timezone.utc)
    with pytest.raises(AppError, match="有效区间不合法"):
        await import_document(db, ADMIN, payload().model_copy(update={
            "effective_from": later, "effective_to": datetime(2026, 10, 1, tzinfo=timezone.utc)}), "trace")


def test_publish_blockers_enforce_review_and_index():
    reviewing = SimpleNamespace(status="reviewing", index_version="text-embedding-v3")
    indexed = [SimpleNamespace(embedding=[0.1], model_version="text-embedding-v3")]
    assert publish_blockers(reviewing, indexed) is None
    assert "提交审核" in publish_blockers(SimpleNamespace(status="draft", index_version="v"), indexed)
    assert "索引构建" in publish_blockers(SimpleNamespace(status="reviewing", index_version=None), indexed)
    assert "没有可发布片段" in publish_blockers(reviewing, [])
    pending = [SimpleNamespace(embedding=None, model_version="")]
    assert "未完成索引构建" in publish_blockers(reviewing, pending)
    # 未配置 Embedding 时的显式降级允许发布（检索端保留纯关键词路径）
    degraded = [SimpleNamespace(embedding=None, model_version="keyword-only")]
    assert publish_blockers(reviewing, degraded) is None


async def test_publish_requires_review_then_allows_keyword_only(knowledge_db):
    session, db = knowledge_db
    document = PolicyDocument(tenant_id="demo", doc_id="refund-policy", title="退货政策", scope="after_sale")
    session.add(document)
    session.flush()
    version = PolicyVersion(tenant_id="demo", policy_document_id=document.id, scope="after_sale",
                            version="v2", status="reviewing", index_version="keyword-only",
                            effective_from=datetime(2026, 10, 1, tzinfo=timezone.utc))
    session.add(version)
    session.flush()
    session.add(PolicyChunk(tenant_id="demo", policy_version_id=version.id, chunk_index=0, section_title="退货",
                            content="退货须在签收后 7 天内提交。", char_start=0, char_end=15,
                            content_hash="h", model_version="keyword-only"))
    session.commit()
    request = SimpleNamespace(state=SimpleNamespace(request_id="trace"))
    result = await publish(version_id=version.id, request=request, actor=ADMIN, db=db)
    assert result["data"]["status"] == "published"
    assert session.query(AuditLog).filter_by(action="knowledge.published").count() == 1


async def test_publish_rejects_draft_and_overlapping_versions(knowledge_db):
    session, db = knowledge_db
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    document = PolicyDocument(tenant_id="demo", doc_id="refund-policy", title="退货政策", scope="after_sale")
    session.add(document)
    session.flush()
    published = PolicyVersion(tenant_id="demo", policy_document_id=document.id, scope="after_sale",
                              version="v1", status="published", index_version="keyword-only",
                              effective_from=start)
    draft = PolicyVersion(tenant_id="demo", policy_document_id=document.id, scope="after_sale",
                          version="v2", status="draft", index_version="keyword-only", effective_from=start)
    session.add_all([published, draft])
    session.flush()
    session.add(PolicyChunk(tenant_id="demo", policy_version_id=draft.id, chunk_index=0, section_title="退货",
                            content="需人工审核。", char_start=0, char_end=6, content_hash="h",
                            model_version="keyword-only"))
    session.commit()
    request = SimpleNamespace(state=SimpleNamespace(request_id="trace"))
    with pytest.raises(Conflict, match="提交审核"):
        await publish(version_id=draft.id, request=request, actor=ADMIN, db=db)
    draft.status = "reviewing"
    session.commit()
    # v1 有效期为开放式，与 v2 重叠 → 必须先撤销或修订
    with pytest.raises(Conflict, match="有效期重叠"):
        await publish(version_id=draft.id, request=request, actor=ADMIN, db=db)


async def test_revoke_marks_version_revoked_with_audit(knowledge_db):
    session, db = knowledge_db
    document = PolicyDocument(tenant_id="demo", doc_id="refund-policy", title="退货政策", scope="after_sale")
    session.add(document)
    session.flush()
    version = PolicyVersion(tenant_id="demo", policy_document_id=document.id, scope="after_sale",
                            version="v1", status="published", index_version="keyword-only",
                            effective_from=datetime(2026, 10, 1, tzinfo=timezone.utc))
    session.add(version)
    session.commit()
    request = SimpleNamespace(state=SimpleNamespace(request_id="trace"))
    result = await revoke(version_id=version.id, request=request, actor=ADMIN, db=db)
    assert result["data"]["status"] == "revoked"
    assert session.query(AuditLog).filter_by(action="knowledge.revoked").count() == 1


async def test_aftersale_without_evidence_or_with_conflict_refuses_to_conclude(monkeypatch):
    monkeypatch.setattr("src.agents.tools.get_settings",
                        lambda: Settings(AGENT_ROUTING_MODE="rules", _env_file=None))
    runtime = AgentRuntime(MagicMock(), BUYER, Chat(conversation_id=uuid4(), client_message_id="m",
                                                    message="尺寸不合适要换货"), "trace")
    runtime.outputs["get_owned_order"] = {"product_id": "p1", "brand": "b", "category": "吸顶灯",
                                          "shop_id": "a", "budget_minor": 29900}
    runtime.outputs["retrieve_policy"] = {"evidence": [], "conflicts": []}
    result = validated_result("aftersale", runtime)
    assert "缺少有效政策依据" in result.message and not result.pending_confirmation
    runtime.outputs["retrieve_policy"] = {"evidence": [{"chunk_id": "c"}],
                                          "conflicts": [{"doc_id": "refund-policy", "versions": ["v1", "v2"]}]}
    result = validated_result("aftersale", runtime)
    assert "存在版本冲突" in result.message and not result.pending_confirmation

def test_effective_ranges_normalize_naive_and_aware_times():
    from src.services.knowledge import parse_effective_range
    start, end = parse_effective_range(datetime(2026,10,1),datetime(2026,11,1,tzinfo=timezone.utc))
    assert start.tzinfo is timezone.utc and end.tzinfo is timezone.utc


async def test_document_scope_cannot_be_changed(knowledge_db):
    _, db = knowledge_db
    await import_document(db, ADMIN, payload(), 'trace')
    with pytest.raises(Conflict, match='适用范围'):
        await import_document(db, ADMIN, payload('v3').model_copy(update={'scope':'payment'}), 'trace')


async def test_knowledge_preview_and_list_respect_tenant(knowledge_db):
    from src.api.knowledge import preview, versions
    from src.core.exceptions import NotFound
    _, db = knowledge_db
    imported = await import_document(db, ADMIN, payload(), 'trace')
    request = SimpleNamespace(state=SimpleNamespace(request_id='trace'))
    result = await versions(request=request,limit=30,offset=0,actor=ADMIN,db=db)
    assert result['data']['items'][0]['id'] == imported['version_id']
    from uuid import UUID
    result = await preview(version_id=UUID(imported['version_id']),request=request,actor=ADMIN,db=db)
    assert result['data']['chunks'] == [] and result['data']['publish_blocker']
    with pytest.raises(NotFound):
        await preview(version_id=UUID(imported['version_id']),request=request,actor=ActorScope('x','other','knowledge_admin',[]),db=db)


def test_chunk_offsets_remain_exact_after_whitespace_only_piece():
    content = '。  a'
    chunks = split_text(content,max_chars=2)
    assert chunks[-1]['char_start'] == 3
    assert all(content[c['char_start']:c['char_end']] == c['content'] for c in chunks)


@pytest.mark.parametrize('limit',[0,-1,True])
def test_invalid_chunk_length_is_rejected(limit):
    with pytest.raises(AppError,match='正整数'):
        split_text('条款',max_chars=limit)


async def test_worker_runs_model_chunking_then_reuses_persisted_chunks(knowledge_db,monkeypatch):
    from src.worker import _knowledge_index
    session, db = knowledge_db
    imported = await import_document(db, ADMIN, payload(), 'trace')
    session.commit()
    from langchain_core.messages import AIMessage
    from src.retrieval.chunking import Segmentation
    parsed = Segmentation(segments=[{'end_unit_id': 0, 'title': '退换货政策'}])
    gateway = SimpleNamespace(structured=AsyncMock(return_value=AIMessage(content='', response_metadata={'parsed': parsed})))
    monkeypatch.setattr('src.retrieval.chunking.get_model_gateway',lambda:gateway)
    monkeypatch.setattr('src.retrieval.chunking.get_settings',lambda:Settings(_env_file=None))
    monkeypatch.setattr('src.worker.get_settings',lambda:Settings(_env_file=None))
    job = session.query(Job).filter_by(job_id=imported['job_id']).one()
    await _knowledge_index(db,job)
    session.commit()
    assert job.status == 'succeeded'
    assert job.payload['chunking']['splitter_version'] == 'langchain-llm-boundaries-v1'
    chunk = session.query(PolicyChunk).one()
    assert chunk.content == payload().content
    assert chunk.model_version == 'keyword-only'
    await _knowledge_index(db,job)
    assert gateway.structured.await_count == 1
    assert session.query(PolicyChunk).count() == 1
