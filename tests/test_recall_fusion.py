"""召回融合与槽位来源（方案 §13/§7）。

覆盖：
- merge_cards：图谱与文本候选按商品去重后统一排序（价格、SKU）再截断
- 导购结果装配：图谱补充候选不重复、不被追加在文本候选之后
- slot_sources：路由槽位取值与来源（payload/message/handoff）可审计
- Supervisor 答案携带 slot_sources
"""
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from src.agents.subagents import validated_result
from src.agents.supervisor import Supervisor
from src.agents.tools import AgentRuntime
from src.api.schemas import Chat
from src.core.config import Settings
from src.core.deps import ActorScope
from src.services.catalog import merge_cards

BUYER = ActorScope("buyer", "tenant", "buyer", ["a"])


def request(message="预算 500 元以内的吸顶灯", **kwargs):
    return Chat(conversation_id=uuid4(), client_message_id="m", message=message, **kwargs)


def card(product_id, price, sku_suffix="41"):
    return {"product_id": product_id, "sku_id": f"{product_id}-{sku_suffix}",
            "title": product_id, "price_minor": price, "available_qty": 1}


def runtime(message="预算 500 元以内的吸顶灯", **payload_kwargs):
    return AgentRuntime(MagicMock(), BUYER, request(message, **payload_kwargs), "trace")


def test_merge_cards_deduplicates_by_product():
    merged = merge_cards([card("p1", 300)], [card("p1", 999), card("p2", 500)])
    assert [item["product_id"] for item in merged] == ["p1", "p2"]
    # 去重保留先出现的候选，价格不被图谱来源覆盖
    assert merged[0]["price_minor"] == 300


def test_merge_cards_sorts_across_sources():
    text = [card("p2", 500), card("p3", 800)]
    graph = [card("p1", 100), card("p2", 500)]
    merged = merge_cards(text, graph)
    # 统一排序后图谱补充的低价商品排在前，而非追加在末尾
    assert [item["product_id"] for item in merged] == ["p1", "p2", "p3"]


def test_merge_cards_caps_at_limit():
    merged = merge_cards([card(f"p{index}", index * 100) for index in range(1, 6)])
    assert [item["product_id"] for item in merged] == ["p1", "p2", "p3"]


def test_shopping_result_merges_graph_and_text_candidates():
    rt = runtime()
    rt.outputs["search_products"] = {"cards": [card("p2", 500)]}
    rt.outputs["graph_lookup"] = {
        "relations": [{"product_id": "p1", "verified": True}],
        "verified_products": [card("p1", 100), card("p2", 500)],
    }
    result = validated_result("shopping", rt)
    assert [item["product_id"] for item in result.cards] == ["p1", "p2"]


def test_slot_sources_track_message_parsed_slots():
    assert runtime().slot_sources() == {
        "category": {"value": "吸顶灯", "source": "message"},
        "budget_minor": {"value": 50000, "source": "message"},
    }


def test_slot_sources_track_payload_slots():
    rt = runtime("查看订单", category="壁灯", budget_minor=10000, target_size="42")
    sources = rt.slot_sources()
    assert sources["category"] == {"value": "壁灯", "source": "payload"}
    assert sources["budget_minor"] == {"value": 10000, "source": "payload"}
    assert sources["target_size"] == {"value": "42", "source": "payload"}
    assert "order_item_id" not in sources


def test_slot_sources_keep_tightest_budget_across_sources():
    rt = runtime("换购", budget_minor=30000, target_size="42")
    rt.handoff = {"budget_minor": 20000}
    assert rt.slot_sources()["budget_minor"] == {"value": 20000, "source": "handoff"}


def test_slot_sources_handoff_overrides_request_values():
    rt = runtime("换大一号", order_item_id=uuid4(), target_size="42")
    rt.handoff = {"category": "吸顶灯", "brand": "栖家灯饰", "target_size": "43"}
    sources = rt.slot_sources()
    assert sources["category"] == {"value": "吸顶灯", "source": "handoff"}
    assert sources["brand"] == {"value": "栖家灯饰", "source": "handoff"}
    # 与工具的尺寸约束一致：移交值覆盖请求值
    assert sources["target_size"] == {"value": "43", "source": "handoff"}
    assert sources["order_item_id"]["source"] == "payload"


async def test_supervisor_answer_records_slot_sources(monkeypatch):
    monkeypatch.setattr("src.agents.tools.get_settings",
                        lambda: Settings(AGENT_ROUTING_MODE="rules", _env_file=None))
    monkeypatch.setattr("src.agents.tools.search_catalog", AsyncMock(return_value=[card("p1", 100)]))
    answer = await Supervisor(MagicMock(), BUYER, "trace").run(request())
    assert answer["routing_mode"] == "langgraph_rules"
    assert answer["slot_sources"]["category"] == {"value": "吸顶灯", "source": "message"}
    assert answer["slot_sources"]["budget_minor"] == {"value": 50000, "source": "message"}