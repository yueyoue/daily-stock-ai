"""T3.5 迁移：新增系统配置表 system_config。

带存在性护栏（同 0002 风格）：新装库由 0001 create_all 直接建出、老库显式补建。
"""
from alembic import op
from sqlalchemy import DateTime, JSON, String, inspect, func

revision = "0003_system_config"
down_revision = "0002_collect_progress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("system_config"):
        return
    op.create_table(
        "system_config",
        op.column("key", String(64), primary_key=True),
        op.column("value", JSON, nullable=False),
        op.column("updated_at", DateTime, server_default=func.now()),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("system_config"):
        op.drop_table("system_config")
