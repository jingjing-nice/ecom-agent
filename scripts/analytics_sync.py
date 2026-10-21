"""仅用于演示：用管理连接重建授权分析投影，查询账号只有 SELECT + FORCE RLS。

不复制真实用户身份、手机号或地址。管理账号用于此离线命令，不交给 Agent。
"""
import asyncio
import hashlib
from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import create_async_engine
from src.core.config import get_settings
from src.core.database import SessionFactory, dispose_engines
from src.models import Order, OrderItem, Product, ProductSKU, InventorySnapshot, RefundItem, Shop

TABLES = {
    "fact_order_items": "order_no text, product_id text, sku_id text, category text, brand text, quantity integer, paid_amount_minor bigint, paid_at timestamptz, region text, age_group text",
    "fact_refunds": "order_no text, category text, refund_qty integer, refund_amount_minor bigint, completed_at timestamptz",
    "dim_product": "product_id text, title text, category text, brand text",
    "dim_sku_inventory": "sku_id text, product_id text, title text, category text, brand text, available_qty integer, observed_at timestamptz",
}


async def main():
    settings = get_settings()
    # URL.set 正确处理带特殊字符的密码；保留业务管理账号，只切换数据库。
    from sqlalchemy.engine import make_url
    admin_url = make_url(settings.db_url).set(database=settings.ANALYTICS_DB_NAME)
    engine = create_async_engine(admin_url)
    async with SessionFactory() as db:
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        snapshot_at = await db.scalar(text("SELECT CURRENT_TIMESTAMP"))
        shops = list((await db.scalars(select(Shop))).all())
        products = (await db.scalars(select(Product).where(Product.status == "on_sale"))).all()
        orders = (await db.execute(select(Order, OrderItem).join(OrderItem, (OrderItem.order_id == Order.id) &
            (OrderItem.tenant_id == Order.tenant_id)).where(Order.paid_at.is_not(None)))).all()
        inventory = (await db.execute(select(Product, ProductSKU, InventorySnapshot).join(ProductSKU,
            (ProductSKU.product_id == Product.product_id) & (ProductSKU.tenant_id == Product.tenant_id)).join(InventorySnapshot,
            (InventorySnapshot.sku_id == ProductSKU.sku_id) & (InventorySnapshot.tenant_id == ProductSKU.tenant_id)).where(
            Product.status == "on_sale", ProductSKU.status == "on_sale"))).all()
        refunds = (await db.execute(select(RefundItem, OrderItem, Order).join(OrderItem,
            (RefundItem.order_item_id == OrderItem.id) & (RefundItem.tenant_id == OrderItem.tenant_id)).join(Order,
            (OrderItem.order_id == Order.id) & (OrderItem.tenant_id == Order.tenant_id)).where(
            RefundItem.status == "completed", RefundItem.completed_at.is_not(None)))).all()
    def anon(order):
        return hashlib.sha256(f"{order.tenant_id}:{order.order_no}".encode()).hexdigest()
    data = {
        "dim_product": [{"tenant_id":p.tenant_id,"shop_id":p.shop_id,"product_id":p.product_id,"title":p.title,"category":p.category,"brand":p.brand} for p in products],
        "fact_order_items": [{"tenant_id":o.tenant_id,"shop_id":o.shop_id,"order_no":anon(o),"product_id":i.product_id,
            "sku_id":i.sku_id,"category":i.category_snapshot,"brand":i.brand_snapshot,"quantity":i.quantity,
            "paid_amount_minor":i.paid_amount_minor,"paid_at":o.paid_at,"region":None,"age_group":None} for o,i in orders],
        "fact_refunds": [{"tenant_id":o.tenant_id,"shop_id":o.shop_id,"order_no":anon(o),"category":i.category_snapshot,
            "refund_qty":r.refund_qty,"refund_amount_minor":r.refund_amount_minor,"completed_at":r.completed_at} for r,i,o in refunds],
        "dim_sku_inventory": [{"tenant_id":p.tenant_id,"shop_id":p.shop_id,"sku_id":s.sku_id,"product_id":p.product_id,
            "title":p.title,"category":p.category,"brand":p.brand,"available_qty":i.available_qty,"observed_at":i.observed_at} for p,s,i in inventory],
    }
    # 配置中的角色名必须作为标识符引用，禁止拼接任意文本。
    role = engine.dialect.identifier_preparer.quote(settings.ANALYTICS_DB_USER)
    async with engine.begin() as conn:
        await conn.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
        await conn.execute(text("CREATE TABLE IF NOT EXISTS analytics_sync_state (tenant_id text NOT NULL, shop_id text NOT NULL, data_as_of timestamptz NOT NULL, PRIMARY KEY (tenant_id, shop_id))"))
        await conn.execute(text("ALTER TABLE analytics_sync_state ENABLE ROW LEVEL SECURITY"))
        await conn.execute(text("ALTER TABLE analytics_sync_state FORCE ROW LEVEL SECURITY"))
        await conn.execute(text("DROP POLICY IF EXISTS tenant_isolation ON analytics_sync_state"))
        await conn.execute(text(f"CREATE POLICY tenant_isolation ON analytics_sync_state FOR SELECT TO {role} USING (tenant_id = current_setting('app.tenant_id', true))"))
        await conn.execute(text("REVOKE ALL ON analytics_sync_state FROM PUBLIC"))
        await conn.execute(text(f"GRANT SELECT ON analytics_sync_state TO {role}"))
        await conn.execute(text("DELETE FROM analytics_sync_state"))
        if shops:
            await conn.execute(text("INSERT INTO analytics_sync_state (tenant_id, shop_id, data_as_of) VALUES (:tenant_id, :shop_id, :data_as_of)"),
                [{"tenant_id": shop.tenant_id, "shop_id": shop.shop_id, "data_as_of": snapshot_at} for shop in shops])
        for table, columns in TABLES.items():
            await conn.execute(text(f"CREATE TABLE IF NOT EXISTS {table} (tenant_id text NOT NULL, shop_id text NOT NULL, {columns})"))
            await conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            await conn.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
            await conn.execute(text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
            await conn.execute(text(f"CREATE POLICY tenant_isolation ON {table} FOR SELECT TO {role} USING (tenant_id = current_setting('app.tenant_id', true))"))
            await conn.execute(text(f"REVOKE ALL ON {table} FROM PUBLIC"))
            await conn.execute(text(f"GRANT SELECT ON {table} TO {role}"))
            await conn.execute(text(f"DELETE FROM {table}"))
            records = data[table]
            if records:
                keys = list(records[0])
                await conn.execute(text(f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join(':'+k for k in keys)})"), records)
    await engine.dispose()
    await dispose_engines()

if __name__ == "__main__":
    asyncio.run(main())
