"""T1.7 迁移：新增断点续采记录表 collect_progress。

说明：0001 用 metadata.create_all 建表（新装库会直接建出本表），故本迁移
带存在性护栏——新装库跳过建表、老库（0001 时无本模型）显式补建，两者都可回溯。
"""
from alembic import op
from sqlalchemy import Date, DateTime, Integer, String, inspect, func

revision = "0002_collect_progress"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("collect_progress"):
        return
    op.create_table(
        "collect_progress",
        op.column("id", Integer, primary_key=True, autoincrement=True),
        op.column("code", String(12), nullable=False, index=True),
        op.column("data_type", String(32), nullable=False, server_default="kline"),
        op.column("first_date", Date, nullable=True),
        op.column("last_date", Date, nullable=True),
        op.column("status", String(16), nullable=False, server_default="partial"),
        op.column("note", String(256), nullable=False, server_default=""),
        op.column("updated_at", DateTime, server_default=func.now()),
    )
    op.create_unique_constraint("uq_progress_code_type", "collect_progress", ["code", "data_type"])


def downgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("collect_progress"):
        op.drop_table("collect_progress")
