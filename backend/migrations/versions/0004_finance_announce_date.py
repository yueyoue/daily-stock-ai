"""财务表新增公告日期 announce_date（财务数据管线 v1 配套）。

带存在性护栏（同 0002/0003 风格）：新装库由 0001 create_all 直接建出、老库显式补列。
字段用途：回测按"当时是否已披露"取数（point-in-time），避免用报告期代替公告日
造成前视偏差（4 月底披露的年报被当成 1 月已知）。

数据来源：东方财富「业绩报表」接口 RPT_LICO_FN_CPD 的 NOTICE_DATE，
按 (code, report_date) 回填；由 backfill_finance.py / incremental_finance.py 调用
app.services.finance.set_announce_dates 写入。
"""
import sqlalchemy as sa
from alembic import op

revision = "0004_finance_announce_date"
down_revision = "0003_system_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "stock_finance" not in insp.get_table_names():
        return  # 新装库由 0001 的 create_all 建表，模型定义里已带该列
    cols = {c["name"] for c in insp.get_columns("stock_finance")}
    if "announce_date" in cols:
        return
    op.add_column("stock_finance", sa.Column("announce_date", sa.Date(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "stock_finance" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("stock_finance")}
    if "announce_date" in cols:
        op.drop_column("stock_finance", "announce_date")