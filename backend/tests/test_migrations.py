"""T1.1 验收：alembic upgrade head 一次建齐 §6 全部表；stock_kline 为 (code,date) 联合主键。"""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.core.config import get_settings

BACKEND_DIR = Path(__file__).resolve().parent.parent

EXPECTED_TABLES = {
    "stock_info", "stock_kline", "stock_finance",
    "stock_fin_indicator", "stock_tech_indicator",
    "market_daily", "report", "stock_strategy", "backtest_record",
    "hot_topic", "news", "watch_list",
    "data_source_log", "data_reconcile", "data_source_config",
    "trade_calendar", "admin_user", "task_log", "collect_progress",
}


def _upgrade_and_inspect():
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    command.upgrade(cfg, "head")
    engine = create_engine(get_settings().database_url)
    return inspect(engine)


def test_alembic_creates_all_tables():
    insp = _upgrade_and_inspect()
    tables = set(insp.get_table_names())
    missing = EXPECTED_TABLES - tables
    assert not missing, f"缺少表: {missing}"


def test_stock_kline_composite_primary_key():
    insp = _upgrade_and_inspect()
    pk = insp.get_pk_constraint("stock_kline")
    assert set(pk["constrained_columns"]) == {"code", "date"}


def test_stock_kline_has_adj_factor():
    insp = _upgrade_and_inspect()
    cols = {c["name"] for c in insp.get_columns("stock_kline")}
    assert "adj_factor" in cols
