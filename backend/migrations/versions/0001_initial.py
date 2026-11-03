"""初始迁移：一次性建齐开发文档 §6 全部表。

说明：初始版本用 metadata.create_all 建表（18 张表手写 ops 过于冗长）；
后续表结构变更请用显式 op.create_table / op.add_column，保证可回溯。
"""
from alembic import op

import app.models  # noqa: F401
from app.models.base import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
