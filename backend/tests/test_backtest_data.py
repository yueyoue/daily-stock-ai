"""T5.5 验收：回测数据口径（后复权管线/含退市股票池/复权因子缺失跳过并告警）。

真实数据"3 只标的（含分红除权、1 只退市）后复权与行情软件对齐"待部署后
T8.1 演练执行；本层口径正确性由除权日连续性用例等价把关。
"""
from datetime import date

from app.models.base import SessionLocal, init_db
from app.models.market import StockInfo, StockKline
from app.models.system import TaskLog
from app.services.backtest_data import (
    FactorMissingError,
    iter_universe,
    load_series,
    load_universe_series,
)


def setup_function():
    init_db()


def teardown_function():
    """共享测试库：清理行情/股票信息/任务日志。"""
    init_db()
    db = SessionLocal()
    try:
        for model in (StockKline, StockInfo, TaskLog):
            db.query(model).delete()
        db.commit()
    finally:
        db.close()


def _bar(code, d, close, factor=1.0, volume=1000.0):
    return StockKline(code=code, date=d, open=close, high=close * 1.01,
                      low=close * 0.99, close=close, volume=volume,
                      amount=close * volume, adj_factor=factor)


def test_hfq_price_continuous_across_ex_dividend():
    """除权日：不复权价跳空 × 因子跳变 → 后复权价连续（与行情软件后复权口径一致）。"""
    db = SessionLocal()
    try:
        # 10派1：除权日前收盘 10.0，除权日开盘 9.9；累计因子 1.0 → 10/9.9
        step = 10.0 / 9.9
        db.add(_bar("600000", date(2026, 1, 1), 10.0, 1.0))
        db.add(_bar("600000", date(2026, 1, 2), 9.9, step))
        db.add(_bar("600000", date(2026, 1, 3), 10.1, step))
        db.commit()

        series = load_series(db, "600000")
        assert len(series) == 3
        before = series[0]["adj_close"]
        after = series[1]["adj_close"]
        assert abs(before - after) < 1e-9  # 后复权连续：10.0 ≈ 9.9×(10/9.9)
        assert series[1]["adj_close"] == series[1]["close"] * step
        assert series[0]["close"] == 10.0  # 不复权原值保留
    finally:
        db.close()


def test_universe_includes_delisted_by_default():
    db = SessionLocal()
    try:
        db.add(_bar("600000", date(2026, 1, 1), 10.0))
        db.add(_bar("600001", date(2026, 1, 1), 20.0))
        db.add(_bar("000001", date(2026, 1, 1), 5.0))
        db.add(StockInfo(code="600001", name="退市股", is_delisted=True))
        db.commit()

        assert iter_universe(db) == ["000001", "600000", "600001"]  # 含退市（防幸存者偏差）
        assert iter_universe(db, include_delisted=False) == ["000001", "600000"]
    finally:
        db.close()


def test_missing_factor_skips_and_warns():
    db = SessionLocal()
    try:
        db.add(_bar("600000", date(2026, 1, 1), 10.0))
        db.add(_bar("600001", date(2026, 1, 1), 20.0, factor=0.0))  # 因子非法
        db.commit()

        try:
            load_series(db, "600001")
        except FactorMissingError as e:
            assert "复权因子" in str(e)
        else:
            raise AssertionError("因子非法必须报 FactorMissingError")

        data, skipped = load_universe_series(db)
        assert list(data) == ["600000"] and skipped == ["600001"]
        warn = (db.query(TaskLog)
                .filter(TaskLog.task_name == "回测数据:跳过标的",
                        TaskLog.status == "warning").all())
        assert len(warn) == 1 and "600001" in warn[0].message  # 告警留痕（T4.6 可见）
    finally:
        db.close()


def test_load_series_respects_date_range():
    db = SessionLocal()
    try:
        for i in range(1, 6):
            db.add(_bar("600000", date(2026, 1, i), 10.0 + i))
        db.commit()
        series = load_series(db, "600000",
                             start_date=date(2026, 1, 2), end_date=date(2026, 1, 4))
        assert [s["date"] for s in series] == [date(2026, 1, 2), date(2026, 1, 3),
                                               date(2026, 1, 4)]
        assert series[0]["adj_open"] == series[0]["open"]  # 因子 1.0 时后复权=原值
    finally:
        db.close()
