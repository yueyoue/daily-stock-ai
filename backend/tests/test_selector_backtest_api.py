"""T3.3 验收：选股与回测 API（扫描候选+匹配明细 / 20s 预算超时 504 / 回测异步状态机）。

契约以开发文档 §7 为准；扫描执行器 T5.2、回测引擎 T5.5~T5.8 接入后本层不变。
"""
import os
import time
from datetime import date, timedelta

from app.core.config import get_settings
from app.models.base import SessionLocal, init_db
from app.models.market import StockKline
from app.models.system import BacktestRecord, TaskLog

D = date(2026, 11, 3)


def setup_function():
    init_db()


def _clean(db):
    for model in (TaskLog, BacktestRecord, StockKline):
        db.query(model).delete()
    db.commit()


def _login(client):
    r = client.post("/api/auth/login",
                    json={"username": "admin",
                          "password": os.environ["ADMIN_INITIAL_PASSWORD"]})
    assert r.status_code == 200


def _seed_kline(db, code, base=10.0, days=40):
    price = base
    for i in range(days):
        d = D - timedelta(days=days - 1 - i)
        price *= 1.01 if i % 3 else 0.99
        db.add(StockKline(code=code, date=d, open=price, high=price * 1.01,
                          low=price * 0.99, close=price, volume=1000.0 + i,
                          amount=price * 1000, adj_factor=1.0))
    db.commit()


# ---------- 扫描 ----------

def test_scan_candidates_and_match_detail(client, tmp_path, monkeypatch):
    db = SessionLocal()
    try:
        _clean(db)
        _seed_kline(db, "600000", base=10.0)
        _seed_kline(db, "000001", base=20.0)
        import app.services.indicator_cache as ic
        monkeypatch.setattr(ic, "_default_dir", lambda: tmp_path / "empty")
        _login(client)

        r = client.post("/api/selector/scan", json={
            "conditions": [{"factor": "ma5", "op": ">", "value": 15.0}],
            "trade_date": D.isoformat(),
        })
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1 and body["candidates"][0]["code"] == "000001"
        assert body["candidates"][0]["matched"] == ["ma5 > 15.0"]
        assert body["candidates"][0]["metrics"]["ma5"] > 15
        assert body["scan_ms"] >= 0 and body["budget_s"] == 20.0

        # 多条件 AND：000001 同时满足 ma5>15 与 rsi14 非空
        r2 = client.post("/api/selector/scan", json={
            "conditions": [{"factor": "ma5", "op": ">", "value": 15.0},
                           {"factor": "obv", "op": "!=", "value": 0.0}]})
        assert r2.json()["total"] == 1

        bad = client.post("/api/selector/scan",
                          json={"conditions": [{"factor": "不存在", "op": ">", "value": 1}]})
        assert bad.status_code == 400 and "未知因子" in bad.json()["message"]
        bad2 = client.post("/api/selector/scan",
                           json={"conditions": [{"factor": "ma5", "op": "??", "value": 1}]})
        assert bad2.status_code == 400 and "未知比较符" in bad2.json()["message"]
    finally:
        db.close()


def test_scan_timeout_returns_504(client, tmp_path, monkeypatch):
    db = SessionLocal()
    try:
        _clean(db)
        _seed_kline(db, "600000")
        import app.services.indicator_cache as ic
        import app.services.selector as sel
        monkeypatch.setattr(ic, "_default_dir", lambda: tmp_path / "empty")

        def slow_scan(*a, **kw):
            time.sleep(0.05)
            return []

        monkeypatch.setattr(sel, "scan_factor", slow_scan)
        monkeypatch.setenv("SELECTOR_SCAN_BUDGET_S", "0.01")
        get_settings.cache_clear()
        try:
            _login(client)
            r = client.post("/api/selector/scan", json={
                "conditions": [{"factor": "ma5", "op": ">", "value": 1.0}]})
        finally:
            get_settings.cache_clear()
        assert r.status_code == 504  # 扫描超时有明确错误返回
        assert r.json()["code"] == 504 and "扫描超时" in r.json()["message"]
    finally:
        db.close()


# ---------- 回测异步状态机 ----------

def test_backtest_run_state_machine(client):
    db = SessionLocal()
    try:
        _clean(db)
        _login(client)
        r = client.post("/api/backtest/run", json={
            "start_date": "2025-01-01", "end_date": "2026-01-01",
            "initial_capital": 100000, "fee_rate": 0.0005, "slippage": 0.001})
        assert r.status_code == 200
        assert r.json()["status"] == "queued"  # 入队即返回（异步执行）
        bt_id = r.json()["id"]

        # 后台任务完成后 → success（TestClient 在响应后执行 background）
        st = client.get(f"/api/backtest/{bt_id}").json()
        assert st["status"] == "success"
        assert st["result"]["risk_notice"] and st["result"]["engine"] == "skeleton"
        assert set(st["result"]) >= {"equity_curve", "win_rate", "profit_loss_ratio",
                                     "max_drawdown", "trades"}

        assert client.get("/api/backtest/9999").json()["code"] == 404
    finally:
        db.close()


def test_backtest_invalid_params_400(client):
    db = SessionLocal()
    try:
        _clean(db)
        _login(client)
        r = client.post("/api/backtest/run", json={
            "start_date": "2026-01-01", "end_date": "2025-01-01",
            "initial_capital": 100000})
        assert r.status_code == 400 and "end_date" in r.json()["message"]
        r2 = client.post("/api/backtest/run", json={
            "start_date": "2025-01-01", "end_date": "2026-01-01",
            "initial_capital": -1})
        assert r2.status_code == 400
    finally:
        db.close()


def test_backtest_failure_marks_failed_with_reason(monkeypatch):
    import app.services.backtest as bt
    db = SessionLocal()
    try:
        _clean(db)
        row = bt.submit_backtest(db, start_date=date(2025, 1, 1),
                                 end_date=date(2026, 1, 1))
        assert row.status == "queued"

        seen = {}
        orig_engine = bt._engine

        def engine_with_state(rec):
            probe = SessionLocal()
            try:
                seen["status"] = probe.get(BacktestRecord, rec.id).status
            finally:
                probe.close()
            return orig_engine(rec)

        monkeypatch.setattr(bt, "_engine", engine_with_state)
        bt.run_backtest(row.id)
        assert seen["status"] == "running"  # 状态机经过 running
        db.expire_all()
        assert db.get(BacktestRecord, row.id).status == "success"

        # 引擎异常 → failed + result.error
        row2 = bt.submit_backtest(db, start_date=date(2025, 1, 1),
                                  end_date=date(2026, 1, 1))

        def boom(rec):
            raise RuntimeError("模拟引擎崩溃")

        monkeypatch.setattr(bt, "_engine", boom)
        bt.run_backtest(row2.id)
        db.expire_all()
        row2 = db.get(BacktestRecord, row2.id)
        assert row2.status == "failed" and "模拟引擎崩溃" in row2.result["error"]
    finally:
        db.close()
