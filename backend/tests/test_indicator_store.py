"""T2.4 验收：模式 A/B 开关与失效兜底（删缓存仍出结果+告警 / 开关切换两模式一致）。

口径见 app/services/indicator_store.py 模块注释。
"""
from datetime import date, timedelta

from app.core.config import get_settings
from app.models.base import SessionLocal, init_db
from app.models.market import StockKline, StockTechIndicator
from app.models.system import TaskLog
from app.services.indicator_cache import build_daily_cache
from app.services.indicator_store import (
    get_code_indicators,
    store_mode_enabled,
    sync_tech_indicators_all,
    sync_tech_indicators_for_code,
)
from app.services.tech_indicators import FLAT_KEYS

D = date(2026, 11, 3)


def setup_function():
    init_db()


def _clean(db):
    for model in (TaskLog, StockTechIndicator, StockKline):
        db.query(model).delete()
    db.commit()


def _seed(db, code="600000", end=D, days=40):
    price = 10.0
    for i in range(days):
        d = end - timedelta(days=days - 1 - i)
        price *= 1.01 if i % 3 else 0.99
        db.add(StockKline(code=code, date=d, open=price, high=price * 1.01,
                          low=price * 0.99, close=price, volume=1000.0 + i,
                          amount=price * 1000, adj_factor=1.0))
    db.commit()


# ---------- 模式 A：入库 + 增量幂等 ----------

def test_mode_a_sync_and_incremental_idempotent():
    db = SessionLocal()
    try:
        _clean(db)
        _seed(db)
        s1 = sync_tech_indicators_for_code(db, "600000", engine="pandas")
        assert s1["periods"] == 40 and s1["written"] == 40 and s1["skipped"] == 0
        row = db.get(StockTechIndicator, ("600000", D))
        assert set(row.metrics) == set(FLAT_KEYS)

        s2 = sync_tech_indicators_for_code(db, "600000", engine="pandas")
        assert s2["written"] == 0 and s2["skipped"] == 40  # 重复跑不重复写
        assert db.query(StockTechIndicator).filter_by(code="600000").count() == 40

        s3 = sync_tech_indicators_all(db, engine="pandas")
        assert s3["codes"] == 1 and s3["written"] == 0
    finally:
        db.close()


# ---------- 模式 B：读缓存 / 删缓存兜底 ----------

def test_mode_b_cache_hit_then_fallback_with_warning(tmp_path, monkeypatch):
    db = SessionLocal()
    try:
        _clean(db)
        _seed(db)
        cache_dir = tmp_path / "cache"
        build_daily_cache(db, D, engine="pandas", cache_dir=cache_dir)

        # get_code_indicators 走 load_cache 默认目录 → 指到测试目录
        import app.services.indicator_cache as ic
        monkeypatch.setattr(ic, "_default_dir", lambda: cache_dir)

        got = get_code_indicators(db, "600000", trade_date=D, engine="pandas")
        assert got["source"] == "cache"  # 命中缓存

        # 删掉缓存文件 → 仍出结果（走慢路径实时计算）+ task_log 告警
        for p in cache_dir.iterdir():
            p.unlink()
        got2 = get_code_indicators(db, "600000", trade_date=D, engine="pandas")
        assert got2["source"] == "realtime"
        assert got2["metrics"]["ma5"] == got["metrics"]["ma5"]  # 数据一致
        warn = db.query(TaskLog).filter_by(task_name="指标兜底:600000").one()
        assert warn.status == "warning" and "缺缓存" in warn.message
    finally:
        db.close()


# ---------- 开关切换两模式数据一致 ----------

def test_mode_switch_consistent_values(monkeypatch, tmp_path):
    db = SessionLocal()
    try:
        _clean(db)
        _seed(db)
        import app.services.indicator_cache as ic
        monkeypatch.setattr(ic, "_default_dir", lambda: tmp_path / "empty")

        # 模式 B：实时兜底值
        b = get_code_indicators(db, "600000", trade_date=D, engine="pandas")
        assert b["source"] == "realtime"  # 无缓存/无库 → 兜底

        # 模式 A：入库后从库表取
        sync_tech_indicators_for_code(db, "600000", engine="pandas")
        monkeypatch.setenv("TECH_INDICATOR_STORE", "true")
        get_settings.cache_clear()
        try:
            assert store_mode_enabled() is True
            a = get_code_indicators(db, "600000", trade_date=D, engine="pandas")
        finally:
            get_settings.cache_clear()
        assert a["source"] == "db"
        assert a["metrics"] == b["metrics"]  # 两模式数据一致
    finally:
        db.close()


def test_missing_code_returns_none():
    db = SessionLocal()
    try:
        _clean(db)
        assert get_code_indicators(db, "000000", engine="pandas") is None
    finally:
        db.close()
