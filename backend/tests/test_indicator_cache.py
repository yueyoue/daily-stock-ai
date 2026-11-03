"""T2.3 验收：模式 B 指标缓存管线（当日快照/元信息/滚动保留 N 天/读缓存不联网）。

口径见 app/services/indicator_cache.py 模块注释。
"""
import json
from datetime import date, timedelta
from pathlib import Path

from app.models.base import SessionLocal, init_db
from app.models.market import StockKline
from app.models.system import TaskLog
from app.scheduler.indicator_jobs import job_build_indicator_cache
from app.services.indicator_cache import (
    build_daily_cache,
    cleanup_cache,
    list_cache_versions,
    load_cache,
)
from app.services.tech_indicators import FLAT_KEYS, compute_indicators


def setup_function():
    init_db()


def _clean(db):
    for model in (TaskLog, StockKline):
        db.query(model).delete()
    db.commit()


def _seed_kline(db, code, end: date, days=40):
    """连续 days 根日线（end 前 days 个日历日，含 end 当日；已存在的日期跳过）。"""
    existing = {r[0] for r in db.query(StockKline.date).filter_by(code=code).all()}
    price = 10.0
    for i in range(days):
        d = end - timedelta(days=days - 1 - i)
        price *= 1.01 if i % 3 else 0.99
        if d in existing:
            continue
        db.add(StockKline(code=code, date=d, open=price, high=price * 1.01,
                          low=price * 0.99, close=price,
                          volume=1000.0 + i, amount=price * 1000, adj_factor=1.0))
    db.commit()
    return price


def _tmp_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache"


# ---------- 当日快照与元信息 ----------

def test_build_daily_cache_parquet_and_meta(tmp_path):
    db = SessionLocal()
    try:
        _clean(db)
        d = date(2026, 11, 3)
        _seed_kline(db, "600000", d)
        _seed_kline(db, "000001", d)
        db.add(StockKline(code="300000", date=d - timedelta(days=1),
                          open=1, high=1, low=1, close=1,
                          volume=100, amount=100, adj_factor=1))  # 当日无行情
        db.commit()

        cache_dir = _tmp_dir(tmp_path)
        meta = build_daily_cache(db, d, engine="pandas", cache_dir=cache_dir)

        assert meta["date"] == "2026-11-03" and meta["rows"] == 2  # 当日有行情的标的
        assert meta["indicators"] == list(FLAT_KEYS) and meta["engine"] == "pandas"
        parquet_path, meta_path = cache_dir / "indicators_20261103.parquet", \
            cache_dir / "indicators_20261103.meta.json"
        assert parquet_path.exists() and meta_path.exists()
        assert json.loads(meta_path.read_text())["rows"] == 2

        data = load_cache(d, cache_dir=cache_dir)
        codes = {r["code"] for r in data["rows"]}
        assert codes == {"600000", "000001"}  # 300000 当日无行情不入快照

        # 数值与"随查随出"口径一致
        bars = (db.query(StockKline).filter_by(code="600000")
                .order_by(StockKline.date.asc()).all())
        expect = compute_indicators([b.close for b in bars], [b.high for b in bars],
                                    [b.low for b in bars], [b.volume for b in bars],
                                    engine="pandas")
        row = next(r for r in data["rows"] if r["code"] == "600000")
        assert abs(row["ma5"] - expect["ma5"]) < 1e-9
        assert abs(row["obv"] - expect["obv"]) < 1e-6
    finally:
        db.close()


def test_load_cache_returns_none_for_missing(tmp_path):
    assert load_cache(date(2020, 1, 1), cache_dir=_tmp_dir(tmp_path)) is None
    assert load_cache(cache_dir=_tmp_dir(tmp_path)) is None  # 无任何版本 → None


# ---------- 滚动保留 N 天清理 ----------

def test_rolling_retention_keeps_newest_n(tmp_path):
    db = SessionLocal()
    try:
        _clean(db)
        cache_dir = _tmp_dir(tmp_path)
        dates = [date(2026, 10, 28) + timedelta(days=i) for i in range(5)]
        for d in dates:
            _seed_kline(db, "600000", d)
            build_daily_cache(db, d, engine="pandas", cache_dir=cache_dir)
        assert len(list_cache_versions(cache_dir)) == 5

        deleted = cleanup_cache(cache_dir=cache_dir, keep_days=3)
        versions = list_cache_versions(cache_dir)
        assert versions == [dates[4], dates[3], dates[2]]  # 保留最新 3 版（新→旧）
        assert len(deleted) == 4  # 2 版 × (parquet+meta)
        assert load_cache(dates[0], cache_dir=cache_dir) is None
        assert load_cache(cache_dir=cache_dir)["meta"]["date"] == dates[4].isoformat()
    finally:
        db.close()


# ---------- 批处理任务（task_log + 清理） ----------

def test_job_build_cache_logs_and_cleans(tmp_path):
    db = SessionLocal()
    try:
        _clean(db)
        cache_dir = _tmp_dir(tmp_path)
        d1, d2 = date(2026, 11, 2), date(2026, 11, 3)
        _seed_kline(db, "600000", d1)
        build_daily_cache(db, d1, engine="pandas", cache_dir=cache_dir)
        _seed_kline(db, "600000", d2)

        stats = job_build_indicator_cache(db, run_date=d2, keep_days=1,
                                          engine="pandas", cache_dir=cache_dir)
        assert stats["rows"] == 1 and stats["deleted"] == ["indicators_20261102.parquet",
                                                           "indicators_20261102.meta.json"]
        assert len(list_cache_versions(cache_dir)) == 1  # keep_days=1

        top = db.query(TaskLog).filter_by(task_name="指标缓存批处理").one()
        assert top.status == "success" and "rows=1" in top.message
    finally:
        db.close()
