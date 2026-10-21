"""图谱候选复核与导购图谱工具（方案 §12 / §13）。

覆盖：
- 图谱候选必须回业务库复核，只有可售项标记 verified 并进入卡片
- 服务端白名单拒绝未知关系类型
- graph_lookup 仅存在于导购工具集，且搭配/替代必须先有参考商品
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from src.agents.subagents import tool_request
from src.agents.tools import AgentRuntime
from src.api.schemas import Chat
from src.core.config import Settings
from src.core.deps import ActorScope
from src.core.exceptions import AppError
from src.models import InventorySnapshot, Product, ProductSKU
from src.services.graph_catalog import graph_candidates

BUYER = ActorScope("buyer", "demo", "buyer", ["a"])


@compiles(JSONB, "sqlite")
def sqlite_json_type(type_, compiler, **kwargs):
    return "JSON"


@pytest.fixture(autouse=True)
def rules_mode(monkeypatch):
    monkeypatch.setattr("src.agents.tools.get_settings", lambda: Settings(AGENT_ROUTING_MODE="rules", _env_file=None))


@pytest.fixture
def catalog_db():
    """p1 可售、p2 零库存、p3 属于其他租户，用于区分关系存在与当前可售。"""
    engine = create_engine("sqlite://")
    for model in (Product, ProductSKU, InventorySnapshot):
        model.__table__.create(engine)
    with Session(engine) as session:
        rows = (("demo", "a", "p1", "吸顶灯", 5),
                ("demo", "a", "p2", "壁灯", 0),
                ("other", "b", "p3", "吸顶灯", 5))
        for tenant, shop, product_id, category, qty in rows:
            session.add(Product(tenant_id=tenant, shop_id=shop, product_id=product_id,
                                title=category, brand="brand", category=category))
            session.add(ProductSKU(tenant_id=tenant, shop_id=shop, product_id=product_id,
                                   sku_id=f"{product_id}-41", specs={"size": "41"}, price_minor=100))
            session.add(InventorySnapshot(tenant_id=tenant, shop_id=shop, sku_id=f"{product_id}-41",
                                          available_qty=qty, observed_at=datetime(2026, 10, 17, tzinfo=timezone.utc)))
        session.commit()
        db = SimpleNamespace(execute=AsyncMock(side_effect=session.execute), scalar=AsyncMock(side_effect=session.scalar))
        yield db
    engine.dispose()


def runtime(message="吸顶灯 42 厘米"):
    return AgentRuntime(MagicMock(), BUYER, Chat(conversation_id=uuid4(), client_message_id="m", message=message), "trace")


async def test_graph_candidates_mark_only_sellable_as_verified(catalog_db, monkeypatch):
    monkeypatch.setattr("src.services.graph_catalog.find_complements", AsyncMock(return_value=[
        {"product_id": "p1", "title": "吸顶灯", "reason": "客厅照明搭配"},
        {"product_id": "p2", "title": "壁灯", "reason": "客厅照明搭配"},
    ]))
    result = await graph_candidates(catalog_db, BUYER, kind="complements", product_ids=["p0"])
    assert [row["verified"] for row in result["relations"]] == [True, False]
    assert [card["product_id"] for card in result["verified_products"]] == ["p1"]
    assert result["unverified_count"] == 1


async def test_graph_candidates_reject_unknown_relation_kind(catalog_db):
    with pytest.raises(AppError):
        await graph_candidates(catalog_db, BUYER, kind="raw_cypher")


async def test_graph_candidates_require_reference_product(catalog_db):
    with pytest.raises(AppError):
        await graph_candidates(catalog_db, BUYER, kind="alternatives")


async def test_graph_lookup_tool_uses_server_side_actor(monkeypatch):
    candidates = AsyncMock(return_value={"kind": "complements", "relations": [], "verified_products": []})
    monkeypatch.setattr("src.agents.tools.graph_candidates", candidates)
    rt = runtime()
    await rt.invoke("shopping", tool_request("graph_lookup", {"kind": "complements", "product_ids": ["p1"]}))
    assert candidates.call_args.kwargs["kind"] == "complements"
    assert candidates.call_args.kwargs["product_ids"] == ["p1"]
    # 授权主体由服务端注入，模型不能传 tenant/actor
    assert "tenant_id" not in rt.registry["shopping"][1].args


async def test_graph_lookup_is_shopping_only():
    rt = runtime()
    assert "graph_lookup" in {tool.name for tool in rt.registry["shopping"]}
    assert "graph_lookup" not in {tool.name for tool in rt.registry["analytics"]}
    assert "graph_lookup" not in {tool.name for tool in rt.registry["aftersale"]}


async def test_cross_tenant_graph_candidate_is_not_verified(catalog_db, monkeypatch):
    """关系指向其他租户商品时，复核必须拒绝，不能进入卡片。"""
    monkeypatch.setattr("src.services.graph_catalog.find_complements", AsyncMock(return_value=[
        {"product_id": "p3", "title": "外部商品"},
    ]))
    result = await graph_candidates(catalog_db, BUYER, kind="complements", product_ids=["p1"])
    assert result["relations"][0]["verified"] is False
    assert result["verified_products"] == []
    assert result["unverified_count"] == 1
    assert BUYER.tenant_id == "demo"

@pytest.mark.parametrize("constraints", [
    {"budget_minor": 99}, {"size": "42"}, {"brand": "other"},
    {"category": "吊灯"}, {"exclude": "p1"},
])
async def test_graph_alternatives_obey_hard_constraints(catalog_db, monkeypatch, constraints):
    monkeypatch.setattr("src.services.graph_catalog.find_alternatives", AsyncMock(return_value=[
        {"product_id": "p1", "reason": "相似款"},
    ]))
    result = await graph_candidates(catalog_db, BUYER, kind="alternatives", product_ids=["p0"], **constraints)
    assert result["verified_products"] == []
    assert result["relations"][0]["verified"] is False


async def test_complements_can_cross_category_but_keep_budget(catalog_db, monkeypatch):
    monkeypatch.setattr("src.services.graph_catalog.find_complements", AsyncMock(return_value=[
        {"product_id": "p1", "reason": "搭配"},
    ]))
    result = await graph_candidates(catalog_db, BUYER, kind="complements", product_ids=["p0"],
                                    category="壁灯", budget_minor=100)
    assert len(result["verified_products"]) == 1
    result = await graph_candidates(catalog_db, BUYER, kind="complements", product_ids=["p0"],
                                    category="壁灯", budget_minor=99)
    assert result["verified_products"] == []


async def test_graph_tool_preserves_aftersale_handoff_constraints(monkeypatch):
    candidates = AsyncMock(return_value={"relations": [], "verified_products": []})
    monkeypatch.setattr("src.agents.tools.graph_candidates", candidates)
    rt = runtime()
    rt.handoff = {"product_id": "p0", "category": "吸顶灯", "brand": "brand",
                  "budget_minor": 20000, "target_size": "42"}
    await rt.invoke("shopping", tool_request("graph_lookup", {"kind": "alternatives"}))
    args = candidates.call_args.kwargs
    assert args["budget_minor"] == 20000
    assert args["size"] == "42"
    assert args["exclude"] == "p0"
    assert args["brand"] == "brand"
