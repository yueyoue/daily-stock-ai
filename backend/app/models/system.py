"""系统类表（开发文档 §6）：接口配置、采集日志、对账、交易日历、任务日志、策略、回测。"""
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DataSourceConfig(Base):
    """数据源接口配置（§2.4 核心）：设置页可增删改，采集器按 数据类型→优先级 选路。"""

    __tablename__ = "data_source_config"
    __table_args__ = (
        UniqueConstraint("data_type", "name", name="uq_source_type_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    data_type: Mapped[str] = mapped_column(String(32), index=True)  # kline/snapshot/finance/...
    name: Mapped[str] = mapped_column(String(64))  # 展示名，如"通达信 mootdx 日线"
    impl_kind: Mapped[str] = mapped_column(String(16), default="py_func")  # py_func / http
    impl_ref: Mapped[str] = mapped_column(String(128))  # "collectors.kline:MootdxKlineCollector" 或 URL
    params_template: Mapped[dict] = mapped_column(JSON, default=dict)
    role: Mapped[str] = mapped_column(String(8), default="备用")  # 主源 / 备用
    priority: Mapped[int] = mapped_column(Integer, default=10)  # 数字越小优先级越高
    qps_limit: Mapped[float] = mapped_column(Float, default=1.0)
    timeout_sec: Mapped[int] = mapped_column(Integer, default=30)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DataSourceLog(Base):
    """数据源采集记录（§6）：每次调用一条。"""

    __tablename__ = "data_source_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    data_type: Mapped[str] = mapped_column(String(32), index=True)
    source_name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), index=True)  # ok / fail
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class DataReconcile(Base):
    """数据交叉对账（§2.3.4）：多源值、差异、对账结果。"""

    __tablename__ = "data_reconcile"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    data_type: Mapped[str] = mapped_column(String(32), index=True)
    data_key: Mapped[str] = mapped_column(String(128), index=True)  # 对账项（如 000001.close@2026-10-31）
    values: Mapped[dict] = mapped_column(JSON, default=dict)  # {源名: 值}
    result_value: Mapped[str] = mapped_column(String(256), default="")
    is_consistent: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TradeCalendar(Base):
    """交易日历（§4.9）：调度前置判断。"""

    __tablename__ = "trade_calendar"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    is_trading_day: Mapped[bool] = mapped_column(Boolean, default=True)


class TaskLog(Base):
    """后台任务日志（§6）。"""

    __tablename__ = "task_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_name: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)  # running / success / failed
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class StockStrategy(Base):
    """用户选股策略（§6）：条件 JSON。"""

    __tablename__ = "stock_strategy"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    conditions: Mapped[dict] = mapped_column(JSON, default=dict)  # 条件 DSL（T5.1）
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class BacktestRecord(Base):
    """回测记录（§6）：策略、区间、结果 JSON。"""

    __tablename__ = "backtest_record"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    strategy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    initial_capital: Mapped[float] = mapped_column(Float, default=100000)
    params: Mapped[dict] = mapped_column(JSON, default=dict)  # 手续费/滑点等
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)  # 净值曲线/胜率/回撤/交易记录
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
