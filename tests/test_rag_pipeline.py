"""真实 LangGraph 节点与 LangChain 接口；模型响应和数据库适配器使用隔离测试桩。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.retrievers import BaseRetriever

from src.core.config import Settings
from src.core.exceptions import WorkflowError
from src.retrieval.chunking import Segmentation, build_chunking_graph, chunk_records
from src.retrieval.rag import GroundedAnswer, PolicyRetriever, build_policy_rag, retrieve_policy


def settings(**kwargs):
    return Settings(_env_file=None,**kwargs)


def parsed_message(parsed):
    """网关 structured() 的测试替身：与真实返回同形（AIMessage + parsed）。"""
    return AIMessage(content='', response_metadata={'parsed': parsed})


def chunk_gateway(segments, **kwargs):
    return SimpleNamespace(structured=AsyncMock(
        return_value=parsed_message(Segmentation.model_validate({'segments': segments, **kwargs}))))


async def test_model_controls_semantic_groups_with_exact_source_offsets():
    content = '退款须凭订单申请。\n\n换货需保持包装完整。'
    gateway = chunk_gateway([{'end_unit_id':0,'title':'售后政策'}])
    graph = build_chunking_graph(gateway=gateway,settings=settings())
    state = await graph.ainvoke({'content':content})
    records = chunk_records(state['documents'])
    assert len(records) == 1 and records[0]['content'] == content
    assert content[records[0]['char_start']:records[0]['char_end']] == records[0]['content']
    assert state['model_calls'] == 1
    assert 'model_semantic_split' in graph.get_graph().nodes
    assert gateway.structured.call_args.kwargs['purpose'] == 'rag.semantic_chunking'


async def test_model_can_split_adjacent_units_by_topic():
    content = '退货条款。'*25+'换货条款。'*25
    # 官方分窗生成的安全单元不由模型重写；响应只选单元 ID。
    from src.retrieval.chunking import split_documents
    units = split_documents(content,max_chars=64)
    segments = [{'end_unit_id':i,'title':f'条款{i}'} for i in range(len(units))]
    gateway = chunk_gateway(segments)
    state = await build_chunking_graph(gateway=gateway,settings=settings()).ainvoke({'content':content})
    assert len(state['documents']) == len(units) > 1
    assert ''.join(d.page_content for d in state['documents']) == content


@pytest.mark.parametrize('segments',[
    [{'end_unit_id':99,'title':'越界'}],
    [{'end_unit_id':0,'title':'重复'},{'end_unit_id':0,'title':'重复'}],
])
async def test_invalid_model_boundaries_never_produce_chunks(segments):
    # 边界不合法属业务校验：同一网关调用返回合法 schema，但分组覆盖失败 → 带错重试两次后拒绝。
    gateway = SimpleNamespace(structured=AsyncMock(side_effect=[
        parsed_message(Segmentation.model_validate({'segments':segments})),
        parsed_message(Segmentation.model_validate({'segments':segments})),
    ]))
    with pytest.raises(WorkflowError,match='边界不合法'):
        await build_chunking_graph(gateway=gateway,settings=settings()).ainvoke({'content':'有效政策'})
    assert gateway.structured.await_count == 2


async def test_empty_segment_list_is_rejected_by_schema():
    # min_length=1 的契约由 schema 在解析层保证；网关解析失败即中断，不产生切片。
    gateway = SimpleNamespace(structured=AsyncMock(side_effect=WorkflowError('模型未返回符合 schema 的结果')))
    with pytest.raises(WorkflowError, match='schema'):
        await build_chunking_graph(gateway=gateway,settings=settings()).ainvoke({'content':'有效政策'})
    assert gateway.structured.await_count == 1


async def test_chunking_budget_is_enforced():
    bad = [{'end_unit_id':0,'title':'未覆盖尾部'}]
    gateway = SimpleNamespace(structured=AsyncMock(
        return_value=parsed_message(Segmentation.model_validate({'segments':bad}))))
    with pytest.raises(WorkflowError,match='预算'):
        await build_chunking_graph(gateway=gateway,settings=settings(CHUNK_MAX_CHARS=500,RAG_CHUNK_MAX_MODEL_CALLS=1)).ainvoke({'content':'甲乙'*60})
    assert gateway.structured.await_count == 1


def row(cid='one',version='v1',content='换货需要保持包装完整。'):
    return {'chunk_id':cid,'policy_version_id':f'policy-{version}','doc_id':'policy','doc_title':'退换货政策',
        'version':version,'scope':'after_sale','section_title':'换货条款','content':content,'char_start':0,'char_end':len(content)}


@pytest.fixture
def search_stubs(monkeypatch):
    keyword, vector = AsyncMock(return_value=[row()]), AsyncMock(return_value=[row()])
    monkeypatch.setattr('src.retrieval.hybrid._keyword_search',keyword)
    monkeypatch.setattr('src.retrieval.hybrid._vector_search',vector)
    monkeypatch.setattr('src.retrieval.rag.get_settings',lambda:settings())
    return keyword,vector


async def test_native_retriever_and_rag_produce_grounded_answer(search_stubs):
    model = AsyncMock(return_value=parsed_message(GroundedAnswer.model_validate(
        {'excerpts':[{'chunk_id':'one','quote':'包装完整'}]})))
    graph = build_policy_rag(MagicMock(),tenant_id='tenant-a',scope='after_sale',answer_model=model)
    state = await graph.ainvoke({'query':'换货需要什么条件'})
    assert state['conflicts'] == [] and '包装完整' in state['answer']
    assert state['hits'][0].keyword_rank == state['hits'][0].vector_rank == 1
    assert all(stub.call_args.kwargs['tenant_id']=='tenant-a' for stub in search_stubs)
    assert issubclass(PolicyRetriever,BaseRetriever)


async def test_conflict_is_checked_before_top_k_and_skips_generation(search_stubs):
    search_stubs[0].return_value = [row(),row('two','v2')]
    model = AsyncMock()
    state = await build_policy_rag(MagicMock(),tenant_id='demo',top_k=1,answer_model=model).ainvoke({'query':'换货'})
    assert state['conflicts'][0]['versions'] == ['v1','v2']
    model.assert_not_awaited()


async def test_rag_rejects_invented_quotes(search_stubs):
    model = AsyncMock(return_value=parsed_message(GroundedAnswer.model_validate(
        {'excerpts':[{'chunk_id':'one','quote':'已退款成功'}]})))
    with pytest.raises(WorkflowError,match='引用不匹配'):
        await retrieve_policy(MagicMock(),'退款',tenant_id='demo',answer_model=model)


async def test_empty_retrieval_does_not_call_model(search_stubs):
    for stub in search_stubs:
        stub.return_value=[]
    model = AsyncMock()
    result = await retrieve_policy(MagicMock(),'缺少依据',tenant_id='demo',answer_model=model)
    assert result['evidence'] == [] and result['answer'] == ''
    model.assert_not_awaited()


@pytest.mark.parametrize('max_chars,end_unit',[(500,0),(64,1)])
async def test_missing_tail_or_oversized_group_is_rejected(max_chars,end_unit):
    # schema 合法但业务边界不合法（未覆盖尾部 / 超最大切片长度）→ 带错重试后拒绝。
    gateway = SimpleNamespace(structured=AsyncMock(return_value=parsed_message(
        Segmentation.model_validate({'segments':[{'end_unit_id':end_unit,'title':'错误分组'}]}))))
    with pytest.raises(WorkflowError,match='边界不合法'):
        await build_chunking_graph(gateway=gateway,settings=settings(CHUNK_MAX_CHARS=max_chars)).ainvoke({'content':'甲'*120})


async def test_model_finds_no_relevant_quote_means_no_policy_basis(search_stubs):
    model=AsyncMock(return_value=parsed_message(GroundedAnswer.model_validate({'excerpts':[]})))
    result=await retrieve_policy(MagicMock(),'无关问题',tenant_id='demo',answer_model=model)
    assert result['evidence'] == [] and result['answer'] == ''


async def test_rag_rejects_unparsable_answer(search_stubs):
    # 测试桩替身不可信：parsed 缺位或非法时中断，不降级为自由文案。
    model = AsyncMock(return_value=AIMessage(content='{"excerpts":[]}'))
    with pytest.raises(WorkflowError, match='schema'):
        await retrieve_policy(MagicMock(),'退款',tenant_id='demo',answer_model=model)

    loose = AsyncMock(return_value=parsed_message(SimpleNamespace(excerpts=[SimpleNamespace(chunk_id='', quote='包装完整')])))
    with pytest.raises(WorkflowError, match='引用不匹配'):
        await retrieve_policy(MagicMock(),'退款',tenant_id='demo',answer_model=loose)
