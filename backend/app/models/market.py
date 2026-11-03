"""行情与财务基础数据表（开发文档 §6）。

- stock_kline：(code, date) 联合主键，存不复权价 + 复权因子（回测口径 §4模块5）
- 财务指标 / 技术指标分表（§6 修订）：报告期粒度 vs 交易日粒度
"""
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class StockInfo(Base):
    """个股基础信息：代码、名称、行业、概念。"""

    __tablename__ = "stock_info"

    code: Mapped[str] = mapped_column(String(12), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    industry: Mapped[str] = mapped_column(String(64), default="")
    concepts: Mapped[list] = mapped_column(JSON, default=list)  # 概念标签列表
    list_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    market: Mapped[str] = mapped_column(String(16), default="")  # 主板/创业板/科创板/北交所
    is_delisted: Mapped[bool] = mapped_column(Boolean, default=False)  # 退市标记（防幸存者偏差）
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class StockKline(Base):
    """日线 K 线（20 年）：不复权 OHLCV + 复权因子。"""

    __tablename__ = "stock_kline"

    code: Mapped[str] = mapped_column(String(12), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0)
    amount: Mapped[float] = mapped_column(Float, default=0)
    adj_factor: Mapped[float] = mapped_column(Float, default=1.0)


class StockFinance(Base):
    """财报原始数据（10 年）：三大报表关键项 + 扩展 JSON。"""

    __tablename__ = "stock_finance"

    code: Mapped[str] = mapped_column(String(12), primary_key=True)
    report_date: Mapped[date] = mapped_column(Date, primary_key=True)  # 报告期
    report_type: Mapped[str] = mapped_column(String(16), default="quarterly")
    total_revenue: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    operating_cashflow: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_assets: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_liabilities: Mapped[float | None] = mapped_column(Float, nullable=True)
    equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps: Mapped[float | None] = mapped_column(Float, nullable=True)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)


class StockFinIndicator(Base):
    """财务指标（30 项，报告期粒度，T2.1 计算）。"""

    __tablename__ = "stock_fin_indicator"

    code: Mapped[str] = mapped_column(String(12), primary_key=True)
    report_date: Mapped[date] = mapped_column(Date, primary_key=True)
    roe: Mapped[float | None] = mapped_column(Float, nullable=True)
    gross_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    debt_to_asset: Mapped[float | None] = mapped_column(Float, nullable=True)
    ocf_to_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)  # 其余指标全量


class StockTechIndicator(Base):
    """技术指标（16 项，交易日粒度；模式 A 入库，§5.4 开关）。"""

    __tablename__ = "stock_tech_indicator"

    code: Mapped[str] = mapped_column(String(12), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)


class MarketDaily(Base):
    """每日大盘汇总：涨跌家数、涨停、情绪分、资金。"""

    __tablename__ = "market_daily"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    up_count: Mapped[int | None] = mapped_column(nullable=True)
    down_count: Mapped[int | None] = mapped_column(nullable=True)
    limit_up_count: Mapped[int | None] = mapped_column(nullable=True)
    limit_down_count: Mapped[int | None] = mapped_column(nullable=True)
    sentiment_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    main_moneyflow: Mapped[float | None] = mapped_column(Float, nullable=True)  # 主力资金净流入（北向口径不可用，§10.7）
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
