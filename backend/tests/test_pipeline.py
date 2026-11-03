"""T1.10 验收：采集流水线（三种模式/清洗入库/task_log 全程记录/触发接口异步执行）。"""
from datetime import date

from app.models.base import SessionLocal, init_db
from app.models.market import MarketDaily, StockInfo, StockKline
from app.models.market import StockFinance
from app.models.system import DataSourceConfig, TaskLog
from app.services.pipeline import run_pipeline
from app.services.source_registry import create_source

from .fake_sources import (
    FakeFinanceCollector,
    FakeKlineCollector,
    FakeMarketDataCollector,
    FakeStockInfoCollector,
)


def setup_function():
    init_db()


def _clean(db):
    for model in (StockKline, StockInfo, StockFinance, MarketDaily,
                  DataSourceConfig, TaskLog):
        db.query(model).delete()
    db.commit()


def _register_fakes(db, kline_codes=("600000",)):
    FakeKlineCollector.reset()
    FakeMarketDataCollector.reset()
    FakeFinanceCollector.reset()
    create_source(db, data_type="stock_info", name="假基础信息源",
                  impl_ref="tests.fake_sources:FakeStockInfoCollector", priority=1)
    create_source(db, data_type="kline", name="假K线源",
                  impl_ref="tests.fake_sources:FakeKlineCollector", priority=1)
    create_source(db, data_type="finance", name="假财务源",
                  impl_ref="tests.fake_sources:FakeFinanceCollector", priority=1)
    for data_type, kind, name in [("moneyflow", "moneyflow", "假资金线"),
                                  ("limit_updown", "limit", "假涨跌停线"),
                                  ("market_summary", "market", "假家数线")]:
        create_source(db, data_type=data_type, name=name,
                      impl_ref="tests.fake_sources:FakeMarketDataCollector",
                      params_template={"kind": kind}, priority=1)
    for code in kline_codes:
        db.add(StockInfo(code=code, name=f"股{code}", list_date=date(2020, 1, 1)))
    db.commit()


def test_single_kline_pipeline_logs_all_stages():
    db = SessionLocal()
    try:
        _clean(db)
        _register_fakes(db)
        stats = run_pipeline(db, "single", "kline", run_date=date(2020, 1, 10))
        assert stats["kline"]["inserted"] == 10  # 假源 10 根日线

        names = [t.task_name for t in db.query(TaskLog).order_by(TaskLog.id.asc()).all()]
        assert "采集任务:single:kline" in names          # 顶层任务
        assert "采集:kline" in names and "入库:kline" in names  # 分阶段记录
        top = db.query(TaskLog).filter_by(task_name="采集任务:single:kline").one()
        assert top.status == "success" and top.duration_ms >= 0
    finally:
        db.close()


def test_incremental_skips_delisted_and_full_backfills():
    db = SessionLocal()
    try:
        _clean(db)
        _register_fakes(db, kline_codes=("600000",))
        db.add(StockInfo(code="600001", name="退市股", list_date=date(2020, 1, 1),
                         is_delisted=True))
        db.commit()

        stats = run_pipeline(db, "incremental", run_date=date(2020, 1, 10))
        assert stats["kline"]["skipped"] == 1  # 退市标的日更跳过
        assert db.query(StockKline).filter_by(code="600001").count() == 0

        stats2 = run_pipeline(db, "single", "kline", run_date=date(2020, 1, 10))
        assert stats2["kline"]["codes"] == 2  # 全量/单类型模式含退市标的（历史回填）
        assert db.query(StockKline).filter_by(code="600001").count() > 0
    finally:
        db.close()


def test_full_pipeline_runs_all_lines_with_stage_logs():
    db = SessionLocal()
    try:
        _clean(db)
        _register_fakes(db)
        stats = run_pipeline(db, "full", run_date=date(2026, 11, 3))
        assert set(stats) >= {"stock_info", "kline", "finance", "market_daily"}
        assert stats["finance"]["inserted"] == 2
        row = db.get(MarketDaily, date(2026, 11, 3))
        assert row.main_moneyflow == 123.4 and row.limit_up_count == 42

        names = [t.task_name for t in db.query(TaskLog).all()]
        for stage in ("采集:stock_info", "入库:stock_info", "采集:kline", "清洗:kline",
                      "入库:kline", "采集:finance", "入库:finance",
                      "采集:market_daily", "入库:market_daily"):
            assert stage in names, f"task_log 缺 {stage}"
    finally:
        db.close()


def test_pipeline_invalid_mode_raises_400():
    db = SessionLocal()
    try:
        _clean(db)
        try:
            run_pipeline(db, "bad-mode")
        except Exception as e:
            assert "400" in str(e) or "mode" in str(e)
        else:
            raise AssertionError("非法 mode 必须报错")
        try:
            run_pipeline(db, "single", "unknown_type")
        except Exception as e:
            assert "data_type" in str(e)
        else:
            raise AssertionError("非法 data_type 必须报错")
    finally:
        db.close()
