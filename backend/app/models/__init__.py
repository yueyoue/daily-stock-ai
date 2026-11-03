"""模型汇总：import 本包即注册全部表（供 Base.metadata / Alembic 使用）。"""
from app.models.base import Base, SessionLocal, engine, get_db, init_db
from app.models.admin_user import AdminUser
from app.models.market import (
    MarketDaily,
    StockFinance,
    StockFinIndicator,
    StockInfo,
    StockKline,
    StockTechIndicator,
)
from app.models.content import HotTopic, News, Report, WatchList
from app.models.system import (
    BacktestRecord,
    DataReconcile,
    DataSourceConfig,
    DataSourceLog,
    StockStrategy,
    TaskLog,
    TradeCalendar,
)

__all__ = [
    "Base",
    "SessionLocal",
    "engine",
    "get_db",
    "init_db",
    "AdminUser",
    "StockInfo",
    "StockKline",
    "StockFinance",
    "StockFinIndicator",
    "StockTechIndicator",
    "MarketDaily",
    "Report",
    "HotTopic",
    "News",
    "WatchList",
    "DataSourceConfig",
    "DataSourceLog",
    "DataReconcile",
    "TradeCalendar",
    "TaskLog",
    "StockStrategy",
    "BacktestRecord",
]
