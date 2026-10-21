"""在 SQLite 上执行实际窗口查询；异步适配仅包装同步 Session。"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from src.core.deps import ActorScope
from src.models import Product, ProductSKU, InventorySnapshot
from src.services.catalog import search_products, has_available_variant


@compiles(JSONB, "sqlite")
def sqlite_json_type(type_, compiler, **kwargs):
    return "JSON"


@pytest.fixture
def catalog():
    engine = create_engine("sqlite://")
    for model in (Product, ProductSKU, InventorySnapshot):
        model.__table__.create(engine)
    with Session(engine) as session:
        for number, count in [(1, 25), (2, 1), (3, 1)]:
            session.add(Product(tenant_id="demo", shop_id="a", product_id=f"p{number}",
                                title=f"商品{number}", brand="brand", category="吸顶灯"))
            for index in range(count):
                sku_id = f"p{number}-{index:02}"
                session.add(ProductSKU(tenant_id="demo", shop_id="a", product_id=f"p{number}",
                                       sku_id=sku_id, specs={"size":"42"}, price_minor=number*100))
                session.add(InventorySnapshot(tenant_id="demo", shop_id="a", sku_id=sku_id,
                    available_qty=1, observed_at=datetime(2026,10,17,tzinfo=timezone.utc)))
        session.commit()
        db = SimpleNamespace(execute=AsyncMock(side_effect=session.execute), scalar=AsyncMock(side_effect=session.scalar))
        yield session, db
    engine.dispose()


async def test_many_skus_do_not_crowd_out_other_products(catalog):
    _, db = catalog
    cards = await search_products(db, ActorScope("buyer","demo","buyer",["a"]))
    assert [card["product_id"] for card in cards] == ["p1","p2","p3"]
    assert cards[0]["sku_id"] == "p1-00"


async def test_hard_constraints_are_applied_before_ranking(catalog):
    _, db = catalog
    actor = ActorScope("buyer","demo","buyer",["a"])
    assert len(await search_products(db, actor, budget_minor=200, size="42")) == 2
    assert await search_products(db, actor, size="41") == []
    assert await search_products(db, ActorScope("buyer","other","buyer",["a"])) == []
    assert await search_products(db, ActorScope("buyer","demo","buyer",["b"])) == []


async def test_off_shelf_product_is_not_an_available_exchange_variant(catalog):
    session, db = catalog
    actor = ActorScope("buyer","demo","buyer",["a"])
    assert await has_available_variant(db, actor, product_id="p1", shop_id="a", size="42")
    product = session.query(Product).filter_by(product_id="p1").one()
    product.status = "off_sale"
    session.commit()
    assert not await has_available_variant(db, actor, product_id="p1", shop_id="a", size="42")


async def test_keyword_retrieval_selects_relevant_products_after_constraints(catalog, monkeypatch):
    from src.core.config import Settings
    session, db = catalog
    monkeypatch.setattr('src.services.product_search.get_settings', lambda: Settings(_env_file=None))
    session.query(Product).filter_by(product_id='p3').one().description = '适合长距离马拉松训练'
    session.commit()
    actor = ActorScope('buyer','demo','buyer',['a'])
    cards = await search_products(db,actor,query='马拉松训练',budget_minor=500)
    assert [c['product_id'] for c in cards] == ['p3']
    assert cards[0]['retrieval']['sources'] == ['keyword']
    assert all(c['price_minor'] <= 200 for c in await search_products(db,actor,query='马拉松训练',budget_minor=200))
    assert await search_products(db,ActorScope('buyer','other','buyer',['a']),query='马拉松训练') == []


async def test_embedding_failure_preserves_keyword_search(catalog, monkeypatch):
    from src.core.config import Settings
    _, db = catalog
    monkeypatch.setattr('src.services.product_search.get_settings',lambda:Settings(DASHSCOPE_API_KEY='test',_env_file=None))
    monkeypatch.setattr('src.services.product_search.get_model_gateway',lambda:SimpleNamespace(embed=AsyncMock(side_effect=RuntimeError('offline'))))
    cards = await search_products(db,ActorScope('buyer','demo','buyer',['a']),query='吸顶灯')
    assert len(cards) == 3
    assert all(c['retrieval']['sources'] == ['keyword'] for c in cards)


@pytest.mark.parametrize('vectors,count,dim', [([],1,2), ([[1.0]],1,2), ([[float('nan'),0.0]],1,2), ([[float('inf'),0.0]],1,2)])
def test_product_index_rejects_invalid_embeddings(vectors,count,dim):
    from src.services.product_search import valid_vectors
    with pytest.raises(ValueError):
        valid_vectors(vectors,count,dim)


async def test_product_detail_is_scoped_and_uses_live_sellable_facts(catalog):
    from src.api.routes import product_detail
    from src.models import Review
    from src.core.exceptions import NotFound
    session, db = catalog
    Review.__table__.create(session.bind)
    session.add(Review(tenant_id='demo',shop_id='a',product_id='p1',rating=4,content='试穿合脚'))
    session.commit()
    request = SimpleNamespace(state=SimpleNamespace(request_id='trace'))
    actor = ActorScope('buyer','demo','buyer',['a'])
    result = await product_detail('p1',request,actor,db)
    assert result['data']['available_offer'][0]['price_minor'] == 100
    assert result['data']['reviews']['samples'] == ['试穿合脚']
    with pytest.raises(NotFound):
        await product_detail('p1',request,ActorScope('buyer','other','buyer',['a']),db)
