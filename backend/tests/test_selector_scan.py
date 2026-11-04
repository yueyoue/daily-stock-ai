"""T5.2 验收：全市场扫描执行器（区间条件/财务因子/清单外因子实时兜底标注/20s 预算）。

缓存快照缺失时走 T2.5 实时计算慢路径（CI 有 pandas/TA-Lib，与 T3.3 测试同法）；
纯函数 compute_factor 与执行器接线分别单测。
"""
from datetime import date, timedelta

from app.models.base import SessionLocal, init_db
from app.models.market import StockFinIndicator, StockKline
from app.models.system import TaskLog
from app.services.factor_runtime import compute_factor, ema, rsi, sma
from app.services.selector import run_scan

D = date(2026, 11, 3)


def setup_function():
    init_db()


def _clean(db):
    for model in (StockKline, StockFinIndicator, TaskLog):
        db.query(model).delete()
    db.commit()


def _seed_kline(db, code, base=10.0, days=40):
    price = base
    for i in range(days):
        d = D - timedelta(days=days - 1 - i)
        price *= 1.01 if i % 3 else 0.99
        db.add(StockKline(code=code, date=d, open=price, high=price * 1.01,
                          low=price * 0.99, close=price, volume=1000.0 + i,
                          amount=price * 1000, adj_factor=1.0))
    db.commit()


def _no_cache(monkeypatch, tmp_path):
    import app.services.indicator_cache as ic
    monkeypatch.setattr(ic, "_default_dir", lambda: tmp_path / "empty")


# ---------- 纯函数：清单外因子实时计算 ----------

def test_factor_runtime_known_values():
    assert sma([1, 2, 3, 4], 3) == 3.0
    assert ema([1, 2, 3, 4], 3) == 3.0  # SMA 播种：e=2，k=0.5 → 4×0.5+2×0.5=3.0
    assert rsi([1, 2, 3, 4, 5], 4) == 100.0  # 全涨 → 100
    assert compute_factor("ma30", [1, 2, 3]) is None  # 序列不足
    assert compute_factor("nope", [1, 2, 3]) is None


def test_factor_runtime_family_dispatch():
    closes = [10.0 + (i % 5) for i in range(40)]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    vols = [100.0] * 40
    assert compute_factor("ma30", closes) == sma(closes, 30)
    assert compute_factor("bias30", closes) == (closes[-1] - sma(closes, 30)) / sma(closes, 30) * 100
    assert compute_factor("roc12", closes) == (closes[-1] / closes[-13] - 1) * 100
    assert compute_factor("mtm12", closes) == closes[-1] - closes[-13]
    assert compute_factor("atr14", closes, highs, lows, vols) is not None
    assert compute_factor("wr14", closes, highs, lows, vols) is not None
    assert compute_factor("cci14", closes, highs, lows, vols) is not None
    assert compute_factor("vr26", closes, highs, lows, vols) is not None
    assert compute_factor("psy12", closes) is not None


# ---------- 执行器 ----------

def test_scan_between_condition_and_matched_detail(tmp_path, monkeypatch):
    db = SessionLocal()
    try:
        _clean(db)
        _no_cache(monkeypatch, tmp_path)
        _seed_kline(db, "600000", base=10.0)
        _seed_kline(db, "000001", base=20.0)

        out = run_scan(db, [{"factor": "ma5", "op": "between", "value": 0, "value2": 100}],
                       trade_date=D)
        assert out["total"] == 2
        assert out["candidates"][0]["matched"] == ["ma5 ∈ [0, 100]"]
        for cand in out["candidates"]:
            assert 0 <= cand["metrics"]["ma5"] <= 100

        # 窄区间只命中一只（000001 的 ma5 明显高于 600000）
        hi = [c for c in out["candidates"] if c["code"] == "000001"][0]["metrics"]["ma5"]
        out2 = run_scan(db, [{"factor": "ma5", "op": "between",
                              "value": hi - 0.5, "value2": hi + 0.5}], trade_date=D)
        assert out2["total"] == 1 and out2["candidates"][0]["code"] == "000001"
    finally:
        db.close()


def test_scan_fin_factor_uses_latest_report_and_skips_missing(tmp_path, monkeypatch):
    db = SessionLocal()
    try:
        _clean(db)
        _no_cache(monkeypatch, tmp_path)
        _seed_kline(db, "600000", base=10.0)
        _seed_kline(db, "000001", base=20.0)
        _seed_kline(db, "300750", base=30.0)  # 无财务数据 → 财务条件下不命中
        db.add(StockFinIndicator(code="600000", report_date=date(2026, 6, 30), roe=1.0))
        db.add(StockFinIndicator(code="600000", report_date=date(2026, 9, 30), roe=15.0))
        db.add(StockFinIndicator(code="000001", report_date=date(2026, 9, 30), roe=5.0,
                                 metrics={"gross_margin": 30.0}))
        db.commit()

        out = run_scan(db, [{"factor": "roe", "op": ">=", "value": 10}], trade_date=D)
        assert out["total"] == 1 and out["candidates"][0]["code"] == "600000"
        assert out["candidates"][0]["metrics"]["roe"] == 15.0  # 取最新报告期（非 6 月的 1.0）
        assert out["candidates"][0]["matched"] == ["roe >= 10"]

        # 多条件 AND：技术 × 财务 组合
        out2 = run_scan(db, [{"factor": "ma5", "op": ">", "value": 5},
                             {"factor": "gross_margin", "op": ">", "value": 20}],
                        trade_date=D)
        assert out2["total"] == 1 and out2["candidates"][0]["code"] == "000001"
    finally:
        db.close()


def test_scan_runtime_factor_fallback_marked(tmp_path, monkeypatch):
    db = SessionLocal()
    try:
        _clean(db)
        _no_cache(monkeypatch, tmp_path)
        _seed_kline(db, "600000", base=10.0)

        out = run_scan(db, [{"factor": "ma30", "op": ">", "value": 5},
                            {"factor": "ma5", "op": ">", "value": 5}],
                       trade_date=D)
        assert out["total"] == 1
        cand = out["candidates"][0]
        assert cand["runtime_computed"] == ["ma30"]  # 清单外因子标注
        assert any("实时计算兜底" in w for w in out["warnings"])

        # 兜底值 = 窗口内收盘 SMA（手工核对口径）
        closes = [r.close for r in db.query(StockKline)
                  .filter(StockKline.code == "600000").order_by(StockKline.date.asc()).all()]
        assert abs(cand["metrics"]["ma30"] - sum(closes[-30:]) / 30) < 1e-9
    finally:
        db.close()
