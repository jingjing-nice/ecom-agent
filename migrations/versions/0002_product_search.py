"""商品可重建向量索引。维度与初始政策索引一致，固定为 1024。"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE product_search_indexes (
        id UUID PRIMARY KEY, tenant_id VARCHAR NOT NULL, product_id VARCHAR(64) NOT NULL,
        content_hash VARCHAR(64) NOT NULL, source_updated_at TIMESTAMPTZ NOT NULL,
        model_version VARCHAR(64) NOT NULL, embedding vector(1024),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_product_search_tenant_pid UNIQUE (tenant_id, product_id),
        FOREIGN KEY (tenant_id, product_id) REFERENCES products(tenant_id, product_id)
    )""")
    op.execute("CREATE INDEX ix_product_search_indexes_tenant_id ON product_search_indexes(tenant_id)")


def downgrade():
    op.drop_table("product_search_indexes")
