from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from src.agents.subagents import tool_request
from src.agents.supervisor import Supervisor
from src.agents.tools import AgentRuntime
from src.api.schemas import Chat
from src.core.config import Settings
from src.core.deps import ActorScope
from src.core.exceptions import PermissionDenied, WorkflowError

BUYER = ActorScope("buyer", "tenant", "buyer", ["a"])


@pytest.fixture(autouse=True)
def rules_mode(monkeypatch):
    monkeypatch.setattr("src.agents.tools.get_settings", lambda: Settings(AGENT_ROUTING_MODE="rules", _env_file=None))


def request(message="500 元以内的吸顶灯", **kwargs):
    return Chat(conversation_id=uuid4(), client_message_id="m", message=message, **kwargs)


def runtime(payload=None):
    return AgentRuntime(MagicMock(), BUYER, payload or request(), "trace")


def model_mode(monkeypatch, **settings):
    monkeypatch.setattr("src.agents.tools.get_settings", lambda: Settings(AGENT_ROUTING_MODE="model", _env_file=None, **settings))


def test_tools_have_standard_schemas_without_actor_parameters():
    rt = runtime()
    for tools in rt.registry.values():
        for tool in tools:
            assert isinstance(tool, BaseTool)
            schema = convert_to_openai_tool(tool)["function"]
            assert schema["name"] == tool.name
            assert not {"tenant_id", "actor", "user_id", "shop_ids", "db"}.intersection(tool.args)
    assert "sql" not in rt.registry["analytics"][1].args


async def test_standard_tool_call_returns_correlated_tool_message(monkeypatch):
    monkeypatch.setattr("src.agents.tools.search_catalog", AsyncMock(return_value=[
        {"product_id": "p1", "sku_id": "s", "price_minor": 29900}]))
    rt = runtime()
    call = tool_request("search_products")
    result = await rt.invoke("shopping", call)
    assert isinstance(result, ToolMessage)
    assert result.tool_call_id == call["id"]
    assert result.artifact["cards"][0]["price_minor"] == 29900
    assert rt.trace[0]["tool"] == "search_products"


async def test_rules_mode_runs_real_parent_and_child_graphs(monkeypatch):
    search = AsyncMock(return_value=[{"product_id": "p1", "sku_id": "s", "price_minor": 29900}])
    monkeypatch.setattr("src.agents.tools.search_catalog", search)
    supervisor = Supervisor(MagicMock(), BUYER, "trace")
    graph, _ = supervisor._build(request())
    assert {"supervisor", "delegates", "finish"}.issubset(graph.get_graph().nodes)
    output = await supervisor.run(request())
    assert output["routing_mode"] == "langgraph_rules"
    assert output["cards"][0]["sku_id"] == "s"
    assert output["model_calls"] == 0
    assert search.call_args.kwargs["budget_minor"] == 50000


async def test_model_supervisor_delegates_and_child_uses_standard_tools(monkeypatch):
    model_mode(monkeypatch)
    model = AsyncMock(side_effect=[
        AIMessage(content="", tool_calls=[tool_request("shopping_agent")]),
        AIMessage(content="", tool_calls=[tool_request("search_products", {"category":"吸顶灯"})]),
        AIMessage(content="模型随意修改价格为1元"), AIMessage(content="完成"),
    ])
    monkeypatch.setattr("src.agents.tools.call_agent_model", model)
    monkeypatch.setattr("src.agents.tools.search_catalog", AsyncMock(return_value=[
        {"product_id": "p1", "sku_id": "s", "price_minor": 29900}]))
    output = await Supervisor(MagicMock(), BUYER, "trace").run(request())
    assert output["routing_mode"] == "langgraph_model" and output["model_calls"] == 4
    assert output["cards"][0]["price_minor"] == 29900
    assert "1元" not in output["message"]
    assert any(isinstance(m, ToolMessage) for m in model.call_args_list[2].args[0])


@dataclass
class Hit:
    doc_id: str = "policy"
    version: str = "v1"
    content: str = "需审核"


async def test_aftersale_handoff_flows_through_supervisor_to_shopping(monkeypatch):
    monkeypatch.setattr("src.agents.tools.owned_item", AsyncMock(return_value=(
        SimpleNamespace(product_id="p1", brand_snapshot="brand", category_snapshot="吸顶灯", quantity=1, paid_amount_minor=29900),
        SimpleNamespace(shop_id="a"))))
    monkeypatch.setattr("src.retrieval.rag.retrieve_policy", AsyncMock(return_value={"evidence":[{"doc_id":"policy","version":"v1","content":"需审核"}],"conflicts":[],"answer":""}))
    monkeypatch.setattr("src.agents.tools.has_available_variant", AsyncMock(return_value=False))
    search = AsyncMock(return_value=[{"product_id": "p2", "sku_id": "alternative", "price_minor": 19900}])
    monkeypatch.setattr("src.agents.tools.search_catalog", search)
    output = await Supervisor(MagicMock(), BUYER, "trace").run(request(
        "换大一号，没货就推荐同品牌替代款", order_item_id=uuid4(), target_size="42", budget_minor=20000))
    assert output["pending_confirmation"]
    assert output["cards"][0]["sku_id"] == "alternative"
    assert [step["agent"] for step in output["trace"]] == ["aftersale"]*3+["shopping"]
    assert search.call_args.kwargs["budget_minor"] == 20000
    assert search.call_args.kwargs["exclude"] == "p1"
    assert search.call_args.kwargs["brand"] == "brand"


async def test_model_cannot_delegate_to_other_role(monkeypatch):
    model_mode(monkeypatch)
    model = AsyncMock(return_value=AIMessage(content="", tool_calls=[tool_request("analytics_agent")]))
    monkeypatch.setattr("src.agents.tools.call_agent_model", model)
    with pytest.raises(PermissionDenied):
        await Supervisor(MagicMock(), BUYER, "trace").run(request())


async def test_duplicate_delegation_is_stopped(monkeypatch):
    model_mode(monkeypatch)
    model = AsyncMock(side_effect=[
        AIMessage(content="", tool_calls=[tool_request("shopping_agent")]),
        AIMessage(content="请明确品类"),
        AIMessage(content="", tool_calls=[tool_request("shopping_agent")]),
    ])
    monkeypatch.setattr("src.agents.tools.call_agent_model", model)
    with pytest.raises(WorkflowError, match="重复移交"):
        await Supervisor(MagicMock(), BUYER, "trace").run(request())


async def test_human_approval_interrupts_and_resumes(monkeypatch):
    monkeypatch.setattr("src.agents.tools.get_settings", lambda: Settings(
        AGENT_ROUTING_MODE="rules", AGENT_HUMAN_APPROVAL=True, _env_file=None))
    monkeypatch.setattr("src.agents.tools.owned_item", AsyncMock(return_value=(
        SimpleNamespace(product_id="p1", brand_snapshot="brand", category_snapshot="吸顶灯", quantity=1, paid_amount_minor=29900),
        SimpleNamespace(shop_id="a"))))
    monkeypatch.setattr("src.retrieval.rag.retrieve_policy", AsyncMock(return_value={"evidence":[{"doc_id":"policy","version":"v1","content":"需审核"}],"conflicts":[],"answer":""}))
    monkeypatch.setattr("src.agents.tools.has_available_variant", AsyncMock(return_value=False))
    monkeypatch.setattr("src.agents.tools.search_catalog", AsyncMock(return_value=[
        {"product_id": "p2", "sku_id": "alternative", "price_minor": 19900}]))
    payload = request("换大一号，没货就推荐同品牌替代款", order_item_id=uuid4(), target_size="42", budget_minor=20000)
    supervisor = Supervisor(MagicMock(), BUYER, "trace")
    paused = await supervisor.run(payload)
    # LangGraph 在审批门 interrupt() 暂停：返回可验证答案并附带恢复所需 thread_id。
    assert paused["requires_human_approval"] and paused["pending_confirmation"] and paused["thread_id"]
    assert "approval" not in paused
    # 凭同一 thread_id 通过 Command(resume=...) 从断点继续。
    final = await supervisor.resume(payload, paused["thread_id"], {"decision": "APPROVED"})
    assert final["approval"]["decision"] == "APPROVED"
    assert "requires_human_approval" not in final
    assert final["cards"][0]["sku_id"] == "alternative"


async def test_shopping_graph_intent_becomes_evidence_and_cards(monkeypatch):
    """显式搭配诉求触发 graph_lookup：关系进证据，已复核商品进卡片（方案 §13）。"""
    monkeypatch.setattr("src.agents.tools.search_catalog", AsyncMock(return_value=[
        {"product_id": "p1", "sku_id": "p1-41", "price_minor": 29900}]))
    monkeypatch.setattr("src.agents.tools.graph_candidates", AsyncMock(return_value={
        "kind": "complements",
        "relations": [{"product_id": "p2", "reason": "客厅照明搭配", "verified": True}],
        "verified_products": [{"product_id": "p2", "sku_id": "p2-41", "price_minor": 4900}],
        "unverified_count": 0,
    }))
    output = await Supervisor(MagicMock(), BUYER, "trace").run(request("500 元以内的吸顶灯，帮我搭配壁灯"))
    assert [step["tool"] for step in output["trace"]] == ["search_products", "graph_lookup"]
    # 图谱候选与文本候选统一按价格升序排序：低价图谱商品排在文本候选之前
    assert [card["product_id"] for card in output["cards"]] == ["p2", "p1"]
    graph_evidence = [item for item in output["evidence"] if item.get("source_type") == "graph"]
    assert graph_evidence and graph_evidence[0]["section_title"] == "客厅照明搭配"
    assert "图谱仅作为搭配或替代依据" in output["message"]


async def test_direct_tool_access_also_checks_role():
    rt = runtime()
    with pytest.raises(PermissionDenied):
        await rt.registry["analytics"][0].ainvoke(tool_request("generate_sql"))


async def test_model_budget_is_shared_across_graphs(monkeypatch):
    model_mode(monkeypatch, TASK_MAX_MODEL_CALLS=2)
    model = AsyncMock(side_effect=[
        AIMessage(content="", tool_calls=[tool_request("shopping_agent")]),
        AIMessage(content="", tool_calls=[tool_request("search_products")]),
    ])
    monkeypatch.setattr("src.agents.tools.call_agent_model", model)
    monkeypatch.setattr("src.agents.tools.search_catalog", AsyncMock(return_value=[]))
    output = await Supervisor(MagicMock(), BUYER, "trace").run(request())
    assert output["model_calls"] == 2 and model.await_count == 2


async def test_external_search_is_a_langchain_tool_with_sources(monkeypatch):
    monkeypatch.setattr("src.agents.tools.get_settings", lambda: Settings(AGENT_ROUTING_MODE="rules", EXTERNAL_SEARCH_ENABLED=True, TAVILY_API_KEY="test", _env_file=None))
    source = {"results":[{"title":"公开资料", "url":"https://example.com", "content":"测试", "source_type":"public_web"}]}
    search = AsyncMock(return_value=source)
    monkeypatch.setattr("src.agents.tools.search_public_information", search)
    rt = runtime()
    message = await rt.invoke("shopping", tool_request("search_external_information", {"query":"吸顶灯资料"}))
    assert message.artifact == source
    rt.payload.order_item_id = uuid4()
    with pytest.raises(PermissionDenied):
        await rt.invoke("shopping", tool_request("search_external_information", {"query":"订单"}))
    assert search.await_count == 1


async def test_invalid_delegation_batch_executes_no_child(monkeypatch):
    model_mode(monkeypatch)
    model = AsyncMock(return_value=AIMessage(content="", tool_calls=[
        tool_request("shopping_agent"), tool_request("analytics_agent"),
    ]))
    search = AsyncMock(return_value=[])
    monkeypatch.setattr("src.agents.tools.call_agent_model", model)
    monkeypatch.setattr("src.agents.tools.search_catalog", search)
    with pytest.raises(PermissionDenied):
        await Supervisor(MagicMock(), BUYER, "trace").run(request())
    assert model.await_count == 1
    search.assert_not_awaited()


@pytest.mark.parametrize("invalid_call", ["execute_readonly", "search_products"])
async def test_invalid_tool_batch_executes_no_tools(monkeypatch, invalid_call):
    model_mode(monkeypatch, AGENT_MAX_TOOL_ROUNDS=1)
    monkeypatch.setattr("src.agents.tools.call_agent_model", AsyncMock(side_effect=[
        AIMessage(content="", tool_calls=[tool_request("shopping_agent")]),
        AIMessage(content="", tool_calls=[tool_request("search_products"), tool_request(invalid_call)]),
        AIMessage(content="汇总"),
    ]))
    search = AsyncMock(return_value=[])
    monkeypatch.setattr("src.agents.tools.search_catalog", search)
    if invalid_call == "execute_readonly":
        with pytest.raises(PermissionDenied):
            await Supervisor(MagicMock(), BUYER, "trace").run(request())
    else:
        result = await Supervisor(MagicMock(), BUYER, "trace").run(request())
        assert result["cards"] == []
    search.assert_not_awaited()


async def test_order_question_needs_no_order_selection(monkeypatch):
    from src.agents.subagents import build_subagent
    rt=runtime(request('查询我的订单'))
    async def invoke(agent,call):
        assert agent=='aftersale' and call['name']=='list_orders'
        rt.outputs['list_orders']={'kind':'orders','scope':'本人订单','orders':[]}
        return ToolMessage(content='orders',tool_call_id=call['id'])
    monkeypatch.setattr(rt,'invoke',invoke)
    output = await build_subagent('aftersale',rt).ainvoke({'messages':[],'rounds':0})
    assert output['result']['data']['kind']=='orders'


async def test_ops_can_route_between_all_three_agents(monkeypatch):
    from src.agents.intent import analytics_question,order_question
    ops=ActorScope('operator','tenant','ops',['a'])
    rt=AgentRuntime(MagicMock(),ops,request(),'trace')
    for name in ('shopping','aftersale','analytics'):
        rt.authorize(name)
    assert analytics_question('按品类统计支付金额')
    assert order_question('查询 DEMO-0000 的订单状态')
    assert not analytics_question('500 元以内的吸顶灯')
    assert not order_question('这个订单可以退货吗')


async def test_rebuilt_graph_restores_delegation_facts_and_budget(monkeypatch):
    from src.agents.checkpoint import new_thread_id, thread_config
    model_mode(monkeypatch)
    model = AsyncMock(side_effect=[
        AIMessage(content="", tool_calls=[tool_request("shopping_agent")]),
        AIMessage(content="", tool_calls=[tool_request("search_products")]),
        AIMessage(content="完成"), AIMessage(content="完成"),
    ])
    monkeypatch.setattr("src.agents.tools.call_agent_model", model)
    search = AsyncMock(return_value=[{"product_id": "p", "sku_id": "s", "price_minor": 100}])
    monkeypatch.setattr("src.agents.tools.search_catalog", search)
    payload, thread = request(), new_thread_id()
    first = Supervisor(MagicMock(), BUYER, "trace")
    graph, _ = first._build(payload, thread)
    config = thread_config(thread)
    paused = await graph.ainvoke(first._initial_state(payload), config, interrupt_after=["delegates"])
    assert paused["runtime"]["model_calls"] == 3
    rebuilt, _ = Supervisor(MagicMock(), BUYER, "trace")._build(payload, thread)
    final = await rebuilt.ainvoke(None, config)
    assert final["answer"]["cards"][0]["sku_id"] == "s"
    assert final["answer"]["model_calls"] == 4
    assert final["answer"]["trace"][0]["tool"] == "search_products"
    assert search.await_count == 1


async def test_resume_checks_owner_request_and_decision(monkeypatch):
    from src.agents.checkpoint import new_thread_id, thread_config
    monkeypatch.setattr("src.agents.tools.get_settings", lambda: Settings(
        AGENT_ROUTING_MODE="rules", AGENT_HUMAN_APPROVAL=True, _env_file=None))
    payload, thread = request(), new_thread_id()
    supervisor = Supervisor(MagicMock(), BUYER, "trace")
    graph, _ = supervisor._build(payload, thread)
    initial = supervisor._initial_state(payload)
    initial["answer"] = {"message": "咨询参考", "pending_confirmation": True, "cards": []}
    await graph.aupdate_state(thread_config(thread), initial, as_node="finish")
    await graph.ainvoke(None, thread_config(thread))
    with pytest.raises(WorkflowError, match="审批决策"):
        await supervisor.resume(payload, thread, {"decision": "invalid"})
    with pytest.raises(PermissionDenied):
        await Supervisor(MagicMock(), ActorScope("other", "tenant", "buyer", ["a"]), "trace").resume(
            payload, thread, {"decision": "APPROVED"})
    with pytest.raises(PermissionDenied):
        await supervisor.resume(payload.model_copy(update={"message": "另一个问题"}), thread, {"decision": "APPROVED"})
    decision = {"decision": "REJECTED", "by": BUYER.user_id}
    final = await Supervisor(MagicMock(), BUYER, "new-trace").resume(payload, thread, decision)
    assert not final["pending_confirmation"]
    assert "已取消" in final["message"]
    assert await supervisor.resume(payload, thread, decision) == final
    with pytest.raises(WorkflowError, match="已经处理"):
        await supervisor.resume(payload, thread, {"decision": "APPROVED"})


async def test_transient_model_retry_counts_attempts_and_never_retries_tools(monkeypatch):
    model_mode(monkeypatch)
    transient = WorkflowError('模型调用失败')
    transient.__cause__ = TimeoutError()
    call = AsyncMock(side_effect=[transient, AIMessage(content='成功')])
    monkeypatch.setattr('src.agents.tools.call_agent_model', call)
    rt = runtime()
    assert (await rt.model([], [], 'test')).content == '成功'
    assert rt.model_calls == 2 and call.await_count == 2
    assert rt.outputs == {} and rt.trace == []


async def test_model_retry_respects_remaining_budget_and_does_not_retry_schema_errors(monkeypatch):
    model_mode(monkeypatch, TASK_MAX_MODEL_CALLS=1)
    error = WorkflowError('模型调用失败')
    error.__cause__ = TimeoutError()
    call = AsyncMock(side_effect=error)
    monkeypatch.setattr('src.agents.tools.call_agent_model', call)
    rt = runtime()
    with pytest.raises(WorkflowError):
        await rt.model([], [], 'test')
    assert rt.model_calls == 1 and call.await_count == 1
    model_mode(monkeypatch)
    call.reset_mock(side_effect=True)
    call.side_effect = WorkflowError('模型返回了无效的工具参数')
    with pytest.raises(WorkflowError):
        await runtime().model([], [], 'test')
    assert call.await_count == 1


async def test_handoff_exposes_only_verified_alternative_search(monkeypatch):
    from src.agents.subagents import build_subagent
    model_mode(monkeypatch)
    rt = runtime(request('缺货就推荐同品牌替代款', target_size='42'))
    rt.handoff = {'product_id': 'original', 'category': '吸顶灯', 'brand': '品牌', 'budget_minor': 29900, 'target_size': '42'}
    model = AsyncMock(side_effect=[AIMessage(content='', tool_calls=[tool_request('search_products')]), AIMessage(content='完成')])
    monkeypatch.setattr('src.agents.tools.call_agent_model', model)
    monkeypatch.setattr('src.agents.tools.search_catalog', AsyncMock(return_value=[
        {'product_id': 'alternative', 'sku_id': 'a-42', 'price_minor': 25900}]))
    output = await build_subagent('shopping', rt).ainvoke({'messages': []})
    assert output['result']['cards'][0]['product_id'] == 'alternative'
    assert all([tool.name for tool in call.args[1]] == ['search_products'] for call in model.call_args_list)
    assert [entry['tool'] for entry in rt.trace] == ['search_products']
