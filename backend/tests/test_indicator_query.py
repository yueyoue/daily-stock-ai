"""T2.5 验收：指标查询服务（单标的查询 / 全市场单因子筛选走缓存 ≤1s / 慢路径兜底）。

口径见 app/services/indicator_query.py 模块注释。
"""
import json
from datetime import date, timedelta

import pandas as pd

from app.core.errors import AppError
from app.models.base import SessionLocal, init_db
from app.models.market import StockKline, StockTechIndicator
from app.models.system import TaskLog
from app.services.indicator_cache import build_daily_cache
from app.services.indicator_query import (
    factor_snapshot,
    query_code,
    scan_factor,
)
from app.services.indicator_store import sync_tech_indicators_for_code
from app.services.tech_indicators import FLAT_KEYS

D = date(2026, 11, 3)


def setup_function():
    init_db()


def _clean(db):
    for model in (TaskLog, StockTechIndicator, StockKline):
        db.query(model).delete()
    db.commit()


def _seed(db, code, base=10.0, end=D, days=40):
    price = base
    for i in range(days):
        d = end - timedelta(days=days - 1 - i)
        price *= 1.01 if i % 3 else 0.99
        db.add(StockKline(code=code, date=d, open=price, high=price * 1.01,
                          low=price * 0.99, close=price, volume=1000.0 + i,
                          amount=price * 1000, adj_factor=1.0))
    db.commit()


# ---------- 单标的查询 ----------

def test_query_code_single_and_factor_filter(tmp_path, monkeypatch):
    db = SessionLocal()
    try:
        _clean(db)
        _seed(db, "600000")
        import app.services.indicator_cache as ic
        monkeypatch.setattr(ic, "_default_dir", lambda: tmp_path / "empty")

        got = query_code(db, "600000", trade_date=D)
        assert got["source"] == "realtime" and set(got["metrics"]) == set(FLAT_KEYS)

        partial = query_code(db, "600000", trade_date=D, factors=["ma5", "rsi14"])
        assert set(partial["metrics"]) == {"ma5", "rsi14"}
        assert partial["metrics"]["ma5"] == got["metrics"]["ma5"]

        try:
            query_code(db, "600000", factors=["不存在的因子"])
        except AppError as e:
            assert e.code == 400
        else:
            raise AssertionError("未知因子必须 400")
        assert query_code(db, "000000") is None
    finally:
        db.close()


# ---------- 全市场单因子筛选（走缓存，≤1s） ----------

def test_scan_factor_full_market_under_1s(tmp_path, monkeypatch):
    db = SessionLocal()
    try:
        _clean(db)
        import app.services.indicator_cache as ic
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir(parents=True)
        # 合成全市场快照（5300 行 × 28 因子），各因子值 = 行号
        rows = [{"code": f"{i:06d}", "date": D.isoformat(),
                 **{k: float(i) for k in FLAT_KEYS}} for i in range(5300)]
        pd.DataFrame(rows, columns=["code", "date", *FLAT_KEYS]).to_parquet(
            cache_dir / "indicators_20261103.parquet", index=False)
        (cache_dir / "indicators_20261103.meta.json").write_text(json.dumps(
            {"date": D.isoformat(), "indicators": list(FLAT_KEYS), "rows": 5300}))
        monkeypatch.setattr(ic, "_default_dir", lambda: cache_dir)

        import time
        t0 = time.monotonic()
        hits = scan_factor(db, "rsi14", ">=", 5299, trade_date=D)
        elapsed = time.monotonic() - t0
        assert [h["code"] for h in hits] == ["005299"]
        assert elapsed < 1.0  # 验收：全市场单因子筛选 ≤1s（实测毫秒级）

        assert len(scan_factor(db, "ma5", ">", 2649, trade_date=D)) == 2650
        assert len(scan_factor(db, "ma5", "!=", 0, trade_date=D)) == 5299
    finally:
        db.close()


def test_scan_factor_values_from_real_cache(tmp_path, monkeypatch):
    db = SessionLocal()
    try:
        _clean(db)
        _seed(db, "600000", base=10.0)
        _seed(db, "000001", base=20.0)
        cache_dir = tmp_path / "cache"
        build_daily_cache(db, D, engine="pandas", cache_dir=cache_dir)
        import app.services.indicator_cache as ic
        monkeypatch.setattr(ic, "_default_dir", lambda: cache_dir)

        data = factor_snapshot(db, trade_date=D)
        assert data["meta"]["rows"] == 2
        vals = {r["code"]: r["ma5"] for r in data["rows"]}
        mid = (vals["600000"] + vals["000001"]) / 2

        hits = scan_factor(db, "ma5", ">=", mid, trade_date=D)
        assert [h["code"] for h in hits] == ["000001"]  # 高价股 ma5 更大
        hits2 = scan_factor(db, "ma5", "<", mid, trade_date=D)
        assert [h["code"] for h in hits2] == ["600000"]

        try:
            scan_factor(db, "不存在", ">", 1, trade_date=D)
        except AppError as e:
            assert e.code == 400
        else:
            raise AssertionError("未知因子必须 400")
    finally:
        db.close()


# ---------- 慢路径兜底（缺缓存） ----------

def test_scan_slow_path_without_cache(tmp_path, monkeypatch):
    db = SessionLocal()
    try:
        _clean(db)
        _seed(db, "600000", base=10.0)
        _seed(db, "000001", base=20.0)
        import app.services.indicator_cache as ic
        monkeypatch.setattr(ic, "_default_dir", lambda: tmp_path / "empty")

        hits = scan_factor(db, "ma5", ">", 15.0, trade_date=D)  # 无缓存 → 慢路径
        assert [h["code"] for h in hits] == ["000001"]
        warn = db.query(TaskLog).filter_by(task_name="指标兜底:scan").one()
        assert warn.status == "warning" and "慢路径" in warn.message
    finally:
        db.close()


# ---------- 模式 A 快照一致性 ----------

def test_factor_snapshot_mode_a_matches_mode_b(tmp_path, monkeypatch):
    db = SessionLocal()
    try:
        _clean(db)
        _seed(db, "600000")
        cache_dir = tmp_path / "cache"
        build_daily_cache(db, D, engine="pandas", cache_dir=cache_dir)
        import app.services.indicator_cache as ic
        monkeypatch.setattr(ic, "_default_dir", lambda: cache_dir)
        b_rows = factor_snapshot(db, trade_date=D)["rows"]

        # 模式 A：入库后从库表出快照
        sync_tech_indicators_for_code(db, "600000", engine="pandas")
        from app.core.config import get_settings
        monkeypatch.setenv("TECH_INDICATOR_STORE", "true")
        get_settings.cache_clear()
        try:
            a_snap = factor_snapshot(db, trade_date=D)
        finally:
            get_settings.cache_clear()
        assert a_snap["meta"]["source"] == "db"
        assert a_snap["rows"] == b_rows  # 两模式快照逐值一致
    finally:
        db.close()
