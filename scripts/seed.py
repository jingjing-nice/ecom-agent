"""可重复执行的固定种子沙箱数据，不访问真实电商平台。"""
import asyncio
import hashlib
import random
from datetime import datetime, timedelta, timezone
from uuid import uuid5, NAMESPACE_URL
from src.core.database import SessionFactory, dispose_engines
from src.core.security import hash_password
from src.models import User, Shop, Product, ProductSKU, InventorySnapshot, Order, OrderItem, PolicyDocument, PolicyVersion, PolicyChunk, Review


def uid(name):
    return uuid5(NAMESPACE_URL, "ecom-demo:" + name)


async def seed():
    rng = random.Random(20261017)
    # ID 与商品随机值固定，业务时间相对初始化日，避免演示订单自然超期。
    now = datetime.now(timezone.utc).replace(microsecond=0)
    async with SessionFactory.begin() as db:
        async def put(record):
            existing = await db.get(type(record), record.id)
            if existing is None:
                db.add(record)
                return
            # 固定演示实体原位更新，保留账号、交易状态、库存及政策发布状态。
            if isinstance(record, (User, Order, InventorySnapshot)):
                return
            preserve = {"status", "effective_from", "published_at", "automation_rules"}
            for key, value in vars(record).items():
                if not key.startswith("_") and key not in preserve:
                    setattr(existing, key, value)
            if isinstance(record, PolicyChunk):
                existing.embedding = None

        for tenant in ["demo", "other"]:
            for shop in ["shop-a", "shop-b"]:
                await put(Shop(id=uid(tenant+shop), tenant_id=tenant, shop_id=shop, name=f"{tenant} 家装灯饰店 {shop}"))
            for role in ["buyer", "buyer2", "staff", "ops", "knowledge_admin"]:
                await put(User(id=uid(tenant+role), tenant_id=tenant, email=f"{role}@{tenant}.local",
                            display_name=role, password_hash=hash_password("Demo123!"),
                            role="buyer" if role == "buyer2" else role, shop_ids=["shop-a"]))
            await db.flush()
            for n in range(30):
                shop = "shop-a" if n % 2 == 0 else "shop-b"
                category = ["吸顶灯", "壁灯", "吊灯"][n%3]
                pid = f"P{n:04}"
                await put(Product(id=uid(tenant+pid), tenant_id=tenant, shop_id=shop, product_id=pid,
                               title=f"栖家灯饰 {category} {n:02}", brand="栖家灯饰", category=category,
                               attributes={"颜色": ["黑色", "白色", "原木色"][n % 3],
                                           "适用场景": ["客厅照明", "卧室照明", "餐厅照明"][n % 3], "材质": "铝合金灯体与亚克力灯罩", "光源": "LED", "尺寸单位": "厘米"},
                               description="家装 LED 灯具，铝合金灯体与亚克力灯罩，尺寸按灯体直径（厘米）选择；安装服务未包含在商品价格中，仅供沙箱演示。"))
                await db.flush()
                for size in ["40", "41", "42"]:
                    sid = pid + "-" + size
                    await put(ProductSKU(id=uid(tenant+sid), tenant_id=tenant, shop_id=shop, product_id=pid,
                                      sku_id=sid, specs={"size": size, "尺寸单位": "厘米"}, price_minor=29900 if category=="吸顶灯" else 4900))
                    await put(InventorySnapshot(id=uid(tenant+sid+"stock"), tenant_id=tenant, shop_id=shop,
                              sku_id=sid, available_qty=0 if n==0 and size=="42" else rng.randint(1,80), observed_at=now))
                if n < 12:
                    oid = uid(tenant+f"order{n}")
                    await put(Order(id=oid, tenant_id=tenant, shop_id=shop, order_no=f"DEMO-{n:04}",
                                 buyer_id=str(uid(tenant+("buyer" if n%2==0 else "buyer2"))),
                                 status="paid" if n in {2, 3} else "delivered", paid_at=now-timedelta(days=3),
                                 delivered_at=None if n in {2, 3} else now-timedelta(days=1),
                                 paid_amount_minor=29900 if category=="吸顶灯" else 4900))
                    await db.flush()
                    await put(OrderItem(id=uid(tenant+f"item{n}"), tenant_id=tenant, order_id=oid,
                                     sku_id=pid+"-41", product_id=pid, quantity=1,
                                     paid_amount_minor=29900 if category=="吸顶灯" else 4900,
                                     title_snapshot=f"栖家灯饰 {category}", brand_snapshot="栖家灯饰",
                                     category_snapshot=category, specs_snapshot={"size":"41", "尺寸单位": "厘米"}))
                    # 评价仅作为用户反馈，不替代产品参数，也不构成功效承诺（方案 §4）
                    await put(Review(id=uid(tenant+f"review{n}"), tenant_id=tenant, shop_id=shop, product_id=pid,
                                  rating=[5, 4, 3][n % 3], buyer_ref=f"anon-{n:02}",
                                  content=["灯光柔和，尺寸与标注一致。", "颜色与图片一致。", "包装完好，物流较快。"][n % 3]))
            docid, vid = uid(tenant+"policy"), uid(tenant+"policy-v1")
            content = "沙箱自动售后政策：支付后 14 天内、订单明细实付不超过 50000 分，未发货的已支付订单可自动申请退款，无需额外凭证；已签收或已完成订单可提交退货退款，须上传订单号与明细实付金额一致的原始凭证。系统仅创建售后单，不代表退款或退货已完成。换货自动执行暂未开放；目标尺寸缺货可查询同品牌替代款。"
            checksum = hashlib.sha256(content.encode()).hexdigest()
            await put(PolicyDocument(id=docid, tenant_id=tenant, doc_id="after-sale-demo", title="演示退换货政策", scope="after_sale"))
            await db.flush()
            await put(PolicyVersion(id=vid, tenant_id=tenant, policy_document_id=docid, scope="after_sale", version="demo-v1",
                                 status="published", effective_from=now-timedelta(days=30), checksum=checksum,
                                 index_version="keyword-demo-v1", published_at=now,
                                 automation_rules={"rules": [
                                     {"case_type":"refund", "order_statuses":["paid"],
                                      "max_days_since_payment":14, "max_amount_minor":50000, "require_evidence":False},
                                     {"case_type":"return_refund", "order_statuses":["delivered","completed"],
                                      "max_days_since_payment":14, "max_amount_minor":50000, "require_evidence":True}]} ))
            await db.flush()
            await put(PolicyChunk(id=uid(tenant+"chunk"), tenant_id=tenant, policy_version_id=vid, chunk_index=0,
                               section_title="退货换货", content=content, char_start=0, char_end=len(content), content_hash=checksum))
    await dispose_engines()

if __name__ == "__main__":
    asyncio.run(seed())
