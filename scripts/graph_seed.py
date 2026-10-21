"""商品知识图谱幂等导入（方案 §13）。

- 节点 graph_key = tenant_id + 业务 ID，唯一约束 + MERGE 幂等，可重复运行
- 搭配/替代/场景关系来自自建人工核验规则（沙箱），写 approved 状态与来源
- 品牌关系仅从有来源资料建立；本沙箱只有单一栖家灯饰，故只建 Brand 节点
- 不访问真实电商平台
"""
import asyncio

from sqlalchemy import select

from src.core.database import SessionFactory, dispose_engines
from src.infra.neo4j_client import close_neo4j_driver, get_neo4j_driver
from src.models import Product

# 节点唯一约束（租户 + 业务 ID 组成全局 graph_key）
CONSTRAINTS = (
    "CREATE CONSTRAINT ecom_product_key IF NOT EXISTS FOR (p:Product) REQUIRE p.graph_key IS UNIQUE",
    "CREATE CONSTRAINT ecom_brand_key IF NOT EXISTS FOR (b:Brand) REQUIRE b.graph_key IS UNIQUE",
    "CREATE CONSTRAINT ecom_category_key IF NOT EXISTS FOR (c:Category) REQUIRE c.graph_key IS UNIQUE",
    "CREATE CONSTRAINT ecom_scene_key IF NOT EXISTS FOR (s:Scene) REQUIRE s.graph_key IS UNIQUE",
)

# 人工核验搭配规则：品类组合 → 适用场景
COMPLEMENT_RULES = (
    {"from": "吸顶灯", "to": "壁灯", "scene": "客厅照明", "reason": "客厅照明搭配：吸顶灯与壁灯（沙箱人工规则）"},
    {"from": "吊灯", "to": "壁灯", "scene": "餐厅照明", "reason": "餐厅照明搭配：装饰吊灯与壁灯（沙箱人工规则）"},
)
CATEGORY_SCENES = {"吸顶灯": "客厅照明", "壁灯": "客厅照明", "吊灯": "餐厅照明"}
# 单条规则的单租户搭配关系上限，避免组合爆炸
COMPLEMENT_LIMIT = 60
ALTERNATIVE_LIMIT = 300

_MERGE_PRODUCTS = """
UNWIND $rows AS row
MERGE (p:Product {graph_key: row.key})
SET p.tenant_id = row.tenant_id, p.biz_id = row.biz_id, p.title = row.title,
    p.brand = row.brand, p.category = row.category, p.shop_id = row.shop_id, p.status = 'on_sale'
MERGE (c:Category {graph_key: row.category_key})
SET c.tenant_id = row.tenant_id, c.name = row.category
MERGE (p)-[:BELONGS_TO]->(c)
MERGE (b:Brand {graph_key: row.brand_key})
SET b.tenant_id = row.tenant_id, b.name = row.brand
MERGE (p)-[:HAS_BRAND]->(b)
"""

_MERGE_COMPLEMENTS = """
UNWIND $rows AS row
MATCH (p:Product {graph_key: row.from_key})
MATCH (q:Product {graph_key: row.to_key})
MERGE (p)-[r:GOES_WITH]->(q)
SET r.status = 'approved', r.source = 'sandbox_curated', r.source_id = row.source_id,
    r.reason = row.reason, r.version = 'graph-v1'
"""

_MERGE_ALTERNATIVES = """
UNWIND $rows AS row
MATCH (p:Product {graph_key: row.from_key})
MATCH (q:Product {graph_key: row.to_key})
MERGE (p)-[r:ALTERNATIVE_TO]->(q)
SET r.status = 'approved', r.source = 'sandbox_curated', r.source_id = row.source_id,
    r.reason = row.reason, r.version = 'graph-v1'
"""

_MERGE_SCENES = """
UNWIND $rows AS row
MATCH (p:Product {graph_key: row.key})
MERGE (s:Scene {graph_key: row.scene_key})
SET s.tenant_id = row.tenant_id, s.name = row.scene
MERGE (p)-[r:FIT_SCENE]->(s)
SET r.status = 'approved', r.source = 'sandbox_curated', r.source_id = row.source_id, r.version = 'graph-v1'
"""


def graph_key(tenant_id: str, biz_id: str) -> str:
    return f"{tenant_id}:{biz_id}"


async def load_products() -> list[dict]:
    async with SessionFactory() as db:
        rows = (await db.execute(select(
            Product.tenant_id, Product.product_id, Product.title,
            Product.brand, Product.category, Product.shop_id,
        ).where(Product.status == "on_sale"))).all()
    return [dict(row._mapping) for row in rows]


def product_rows(products: list[dict]) -> list[dict]:
    return [
        {
            "key": graph_key(p["tenant_id"], p["product_id"]),
            "tenant_id": p["tenant_id"], "biz_id": p["product_id"], "title": p["title"],
            "brand": p["brand"], "category": p["category"], "shop_id": p["shop_id"],
            "brand_key": graph_key(p["tenant_id"], f"brand:{p['brand']}"),
            "category_key": graph_key(p["tenant_id"], f"category:{p['category']}"),
        }
        for p in products
    ]


def complement_rows(products: list[dict]) -> list[dict]:
    """按人工核验的品类搭配规则生成关系（同租户、同店铺）。"""
    rows: list[dict] = []
    for rule in COMPLEMENT_RULES:
        sources = [p for p in products if p["category"] == rule["from"]]
        targets = [p for p in products if p["category"] == rule["to"]]
        count = 0
        for source in sources:
            for target in targets:
                if source["tenant_id"] != target["tenant_id"] or source["shop_id"] != target["shop_id"]:
                    continue
                rows.append({
                    "from_key": graph_key(source["tenant_id"], source["product_id"]),
                    "to_key": graph_key(target["tenant_id"], target["product_id"]),
                    "source_id": f"rule:{rule['from']}-{rule['to']}",
                    "reason": rule["reason"],
                })
                count += 1
                if count >= COMPLEMENT_LIMIT:
                    break
            if count >= COMPLEMENT_LIMIT:
                break
    return rows


def alternative_rows(products: list[dict]) -> list[dict]:
    """同租户、同品类、同品牌内的相邻商品视为可替代款（沙箱人工规则）。"""
    groups: dict[tuple, list[dict]] = {}
    for product in products:
        groups.setdefault((product["tenant_id"], product["category"], product["brand"]), []).append(product)
    rows: list[dict] = []
    for items in groups.values():
        items.sort(key=lambda item: item["product_id"])
        for source, target in zip(items, items[1:]):
            rows.append({
                "from_key": graph_key(source["tenant_id"], source["product_id"]),
                "to_key": graph_key(target["tenant_id"], target["product_id"]),
                "source_id": "rule:same-category-brand",
                "reason": "同品类同品牌可替代款（沙箱人工规则）",
            })
    return rows[:ALTERNATIVE_LIMIT]


def scene_rows(products: list[dict]) -> list[dict]:
    rows = []
    for product in products:
        scene = CATEGORY_SCENES.get(product["category"])
        if not scene:
            continue
        rows.append({
            "key": graph_key(product["tenant_id"], product["product_id"]),
            "tenant_id": product["tenant_id"], "scene": scene,
            "scene_key": graph_key(product["tenant_id"], f"scene:{scene}"),
            "source_id": f"rule:scene-{scene}",
        })
    return rows


async def seed():
    products = await load_products()
    if not products:
        raise SystemExit("业务库没有可导入的商品，请先执行 scripts.seed")

    driver = get_neo4j_driver()
    for statement in CONSTRAINTS:
        await driver.execute_query(statement)
    await driver.execute_query(_MERGE_PRODUCTS, product_rows(products))
    await driver.execute_query(_MERGE_SCENES, scene_rows(products))
    await driver.execute_query(_MERGE_COMPLEMENTS, complement_rows(products))
    await driver.execute_query(_MERGE_ALTERNATIVES, alternative_rows(products))
    print(f"图谱导入完成：商品 {len(products)}，节点约束 {len(CONSTRAINTS)}，关系已按 graph_key 幂等写入")
    await close_neo4j_driver()
    await dispose_engines()


if __name__ == "__main__":
    asyncio.run(seed())