"""T3.3 验收：选股与回测 API（扫描候选+匹配明细 / 20s 预算超时 504 / 回测异步状态机）。

契约以开发文档 §7 为准；扫描执行器 T5.2、回测引擎 T5.5~T5.8 接入后本层不变。
T5.8：回测改走统一任务队列（后台线程），HTTP 立即返回 + 进度可查 + 并发闸门排队。
"""
import os
import threading
import time
from datetime import date, timedelta
from types import SimpleNamespace

import app.services.backtest as bt
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


def _wait_terminal(client, bt_id, timeout_s=15.0):
    """轮询回测状态直至终态（T5.8：后台线程执行，需轮询而非依赖响应内联）。"""
    deadline = time.monotonic() + timeout_s
    st = {}
    while time.monotonic() < deadline:
        st = client.get(f"/api/backtest/{bt_id}").json()
        if st.get("status") in ("success", "failed"):
            return st
        time.sleep(0.05)
    return st


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
        assert body["candidates"][0]["matched"] == ["ma5 > 15"]  # describe() 人读格式（T5.2 接入）
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

        monkeypatch.setattr(sel, "_slow_snapshot", slow_scan)  # T5.2 起缺缓存入口
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
        t0 = time.monotonic()
        r = client.post("/api/backtest/run", json={
            "start_date": "2025-01-01", "end_date": "2026-01-01",
            "initial_capital": 100000, "fee_rate": 0.0005, "slippage": 0.001,
            "conditions": {"logic": "and", "conditions": [
                {"factor": "ma5", "op": ">", "value": 0.0}]}})
        assert r.status_code == 200
        # T5.8：投递到后台任务队列后立即返回（不阻塞请求线程）
        assert time.monotonic() - t0 < 5.0
        assert r.json()["status"] == "queued"  # 入队即返回（异步执行）
        assert r.json()["progress"] == {}
        bt_id = r.json()["id"]

        st = _wait_terminal(client, bt_id)
        assert st["status"] == "success"
        assert st["result"]["risk_notice"] and st["result"]["engine"] == "t5.6"
        assert set(st["result"]) >= {"equity_curve", "trades", "events",
                                     "final_equity", "win_rate", "max_drawdown"}
        # T5.8：完成后进度落到 100%
        assert st["progress"]["percent"] == 100.0

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
        row = bt.submit_backtest(
            db, start_date=date(2025, 1, 1), end_date=date(2026, 1, 1),
            params={"conditions": {"logic": "and", "conditions": [
{"factor": "ma5", "op": ">", "value": 0.0}]}})
        assert row.status == "queued"

        seen = {}
        orig_engine = bt._engine

        def engine_with_state(rec, **kwargs):
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

        def boom(rec, **kwargs):
            raise RuntimeError("模拟引擎崩溃")

        monkeypatch.setattr(bt, "_engine", boom)
        bt.run_backtest(row2.id)
        db.expire_all()
        row2 = db.get(BacktestRecord, row2.id)
        assert row2.status == "failed" and "模拟引擎崩溃" in row2.result["error"]
    finally:
        db.close()


# ---------- T5.8：任务队列 / 进度可查 / 并发闸门 ----------

def test_backtest_run_hands_off_to_queue(client, monkeypatch):
    """POST /run 只入队投递即返回，不在请求线程内执行引擎。"""
    db = SessionLocal()
    try:
        _clean(db)
        _login(client)
        handed = {}
        monkeypatch.setattr("app.api.backtest.submit_backtest_task",
                            lambda rid: handed.setdefault("id", rid))
        r = client.post("/api/backtest/run", json={
            "start_date": "2025-01-01", "end_date": "2026-01-01",
            "initial_capital": 100000,
            "conditions": {"logic": "and", "conditions": [
                {"factor": "ma5", "op": ">", "value": 0.0}]}})
        assert r.status_code == 200 and r.json()["status"] == "queued"
        assert handed["id"] == r.json()["id"]  # 已投递给任务队列
        # 未真正执行 → 仍是 queued（证明引擎不在请求线程里跑）
        assert client.get(f"/api/backtest/{handed['id']}").json()["status"] == "queued"
    finally:
        db.close()


def test_backtest_progress_readable_while_running(client):
    """running 期间 GET 可读到引擎写回的进度（长周期可查）。"""
    db = SessionLocal()
    try:
        _clean(db)
        _login(client)
        row = bt.submit_backtest(db, start_date=date(2025, 1, 1),
                                 end_date=date(2026, 1, 1),
                                 params={"conditions": {"logic": "and", "conditions": []}})
        row.status = "running"
        db.commit()
        bt._write_progress(row.id, 30, 120)  # 模拟引擎进度回调写回
        st = client.get(f"/api/backtest/{row.id}").json()
        assert st["status"] == "running"
        assert st["progress"]["done"] == 30 and st["progress"]["total"] == 120
        assert st["progress"]["percent"] == 25.0
    finally:
        db.close()


def test_backtest_queue_gate_keeps_task_queued(monkeypatch):
    """并发槽位占满 → 任务保持 queued（真排队，不无界并发）；释放后继续执行。"""
    bt.reset_queue()
    slots = max(1, int(getattr(get_settings(), "backtest_max_concurrent", 2) or 2))
    gate = bt._queue()
    for _ in range(slots):
        gate.acquire()
    db = SessionLocal()
    thread = None
    try:
        _clean(db)
        row = bt.submit_backtest(db, start_date=date(2025, 1, 1),
                                 end_date=date(2026, 1, 1),
                                 params={"conditions": {"logic": "and", "conditions": []}})
        thread = threading.Thread(target=bt.run_backtest, args=(row.id,), daemon=True)
        thread.start()
        time.sleep(0.3)
        db.expire_all()
        assert db.get(BacktestRecord, row.id).status == "queued"  # 无槽位 → 排队等待
    finally:
        for _ in range(slots):
            gate.release()
        if thread is not None:
            thread.join(timeout=10)
        db.expire_all()
        if thread is not None:
            assert db.get(BacktestRecord, row.id).status in ("success", "failed")
        db.close()
        bt.reset_queue()


def test_backtest_queue_timeout_marks_failed(monkeypatch):
    """排队超过 backtest_queue_timeout_s → failed 并留因。"""
    bt.reset_queue()
    slots = max(1, int(getattr(get_settings(), "backtest_max_concurrent", 2) or 2))
    gate = bt._queue()
    for _ in range(slots):
        gate.acquire()
    monkeypatch.setattr(bt, "get_settings",
                        lambda: SimpleNamespace(backtest_max_concurrent=slots,
                                                backtest_queue_timeout_s=0.05))
    db = SessionLocal()
    try:
        _clean(db)
        row = bt.submit_backtest(db, start_date=date(2025, 1, 1),
                                 end_date=date(2026, 1, 1),
                                 params={"conditions": {"logic": "and", "conditions": []}})
        assert bt.run_backtest(row.id) is False
        db.expire_all()
        rec = db.get(BacktestRecord, row.id)
        assert rec.status == "failed" and "排队" in rec.result["error"]
    finally:
        for _ in range(slots):
            gate.release()
        db.close()
        bt.reset_queue()
