"""构建商品语义索引：python -m scripts.catalog_index --embed。

模型配置就绪才调用 Embedding；按内容哈希及模型版本跳过相同内容，批次失败可重跑。
当前演示规模采用 pgvector 精确搜索，扩容前再评测 ANN。
"""
import argparse
import asyncio
from sqlalchemy import select
from src.core.config import get_settings
from src.core.database import SessionFactory, dispose_engines
from src.models import Product, ProductSearchIndex
from src.services.product_search import content_hash, product_text, valid_vectors


async def build_index(*, embed=False):
    settings = get_settings()
    if not embed or not settings.embedding_api_key:
        print("未启用商品向量索引；关键词与结构化检索仍可用。配置 EMBEDDING_API_KEY 后传 --embed。")
        return
    if settings.EMBEDDING_DIM != 1024:
        raise ValueError("当前迁移固定为 1024 维；更换维度需先新增迁移重建索引")
    from src.infra.model_gateway import get_model_gateway
    updated = 0
    async with SessionFactory() as db:
        products = list((await db.scalars(select(Product).where(Product.status == "on_sale")
            .order_by(Product.tenant_id, Product.product_id))).all())
        for start in range(0, len(products), 20):
            pending = []
            for product in products[start:start+20]:
                index = await db.scalar(select(ProductSearchIndex).where(
                    ProductSearchIndex.tenant_id == product.tenant_id,
                    ProductSearchIndex.product_id == product.product_id).with_for_update())
                digest = content_hash(product)
                if index and index.content_hash == digest and index.model_version == settings.EMBEDDING_MODEL and index.embedding is not None:
                    index.source_updated_at = product.updated_at
                    continue
                pending.append((product, index, digest))
            if pending:
                vectors = await get_model_gateway().embed([product_text(p) for p, _, _ in pending], purpose="catalog_index")
                valid_vectors(vectors, len(pending), settings.EMBEDDING_DIM)
                for (product, index, digest), vector in zip(pending, vectors):
                    if index is None:
                        index = ProductSearchIndex(tenant_id=product.tenant_id, product_id=product.product_id)
                        db.add(index)
                    index.content_hash, index.model_version = digest, settings.EMBEDDING_MODEL
                    index.source_updated_at, index.embedding = product.updated_at, vector
                updated += len(pending)
            await db.commit()
    print(f"商品向量索引更新完成：{updated} 项")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--embed', action='store_true')
    args = parser.parse_args()
    try:
        await build_index(embed=args.embed)
    finally:
        await dispose_engines()


if __name__ == '__main__':
    asyncio.run(main())
