"""为已发布政策绑定显式自动售后规则；已有政策不自动授予执行权限。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('policy_versions', sa.Column('automation_rules', JSONB(), nullable=True))


def downgrade():
    op.drop_column('policy_versions', 'automation_rules')
