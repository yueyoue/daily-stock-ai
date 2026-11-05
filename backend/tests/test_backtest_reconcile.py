"""T5.8 验收：回测对账（独立复算）与异步进度/并发闸门。

覆盖：
- reconcile_backtest 对引擎产出逐项交叉比对（成交价/手续费/涨跌停/T+1/FIFO/现金重放）
- 篡改任一环节 → 对账失败（证明对账有区分力，不是恒真）
- 引擎 progress_cb 逐交易日回传（长周期可查进度的数据源）
- 自检脚本 scripts/reconcile_backtest.py 端到端可跑且对账通过
"""
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from app.services.backtest_engine import TradeRules, simulate
from app.services.backtest_reconcile import reconcile_backtest
from app.services.backtest_stats import compute_stats

BACKEND = Path(__file__).resolve().parents[1]
CAPITAL = 100000.0
RULES_DICT = {"slippage": 0.001, "fee_rate": 0.0005, "limit_pct": 0.10}


def _series(code, days, base, adj=1.0, *, missing=(), limit_up=(), limit_down=()):
    rows, price, last = [], base, None
    d0 = date(2025, 1, 2)
    for i in range(days):
        d = d0 + timedelta(days=i)
        if i in missing:
            continue
        if i in limit_up and last is not None:
            o = c = round(last * 1.10, 2)
        elif i in limit_down and last is not None:
            o = c = round(last * 0.90, 2)
        else:
            o = round(price, 2)
            c = round(price * 1.004, 2)
            price = c
        last = c
        rows.append({"date": d, "open": o, "high": round(max(o, c) * 1.006, 2),
                     "low": round(min(o, c) * 0.994, 2), "close": c, "volume": 1e6,
                     "amount": c * 1e6, "adj_factor": adj, "adj_open": o * adj,
                     "adj_high": o * adj, "adj_low": o * adj, "adj_close": c * adj})
    return rows


def _scenario():
    dates = [date(2025, 1, 2) + timedelta(days=i) for i in range(30)]
    data = {
        "600001": _series("600001", 30, 10.0),
        "600002": _series("600002", 30, 20.0, adj=1.5, limit_up=(13,), limit_down=(21,)),
        "600003": _series("600003", 30, 8.0, adj=2.0, missing=(21, 27)),
    }

    def cand(day):
        i = dates.index(day)
        if i < 4:
            return []
        if i < 12:
            return ["600001"]
        if i < 20:
            return ["600002"]
        if i < 26:
            return ["600003"]
        return ["600001", "600002"]

    return data, cand, dates[0], dates[-1]


def _run(data, cand, start, end, cb=None):
    rules = TradeRules(max_positions=2, **RULES_DICT)
    out = simulate(data, cand, start, end, CAPITAL, rules, progress_cb=cb)
    stats = compute_stats(out["trades"], out["equity_curve"], CAPITAL)
    return {**out, **stats}, out


def test_reconcile_independent_replay_matches_engine():
    data, cand, start, end = _scenario()
    result, out = _run(data, cand, start, end)
    assert out["trades"], "场景应产生成交"
    rep = reconcile_backtest(result, initial_capital=CAPITAL,
                             rules={**RULES_DICT, "max_positions": 2}, bars=data)
    assert rep["consistent"] is True, rep["failed_checks"]
    # 逐项 6 条全部通过
    assert len(rep["checks"]) == 6 and all(c["ok"] for c in rep["checks"])
    # 现金重放与引擎期末资产一致（独立复算确有效果）
    assert abs(rep["replay_equity"] - result["final_equity"]) <= 0.01
    # 场景确实触发了涨跌停/停牌顺延分支（不是空跑）
    kinds = {e["type"] for e in out["events"]}
    assert {"buy_deferred_limit", "sell_deferred_limit",
            "buy_deferred_suspend", "sell_deferred_suspend"} <= kinds


def test_reconcile_flags_tampered_trade_price():
    data, cand, start, end = _scenario()
    result, _ = _run(data, cand, start, end)
    result = dict(result)
    result["trades"] = [dict(t) for t in result["trades"]]
    result["trades"][0]["price"] = round(result["trades"][0]["price"] * 1.05, 6)  # 篡改成交价
    rep = reconcile_backtest(result, initial_capital=CAPITAL,
                             rules={**RULES_DICT, "max_positions": 2}, bars=data)
    assert rep["consistent"] is False
    assert "逐笔成交价（adj_open×(1±滑点)）" in rep["failed_checks"]


def test_reconcile_flags_equity_mismatch():
    data, cand, start, end = _scenario()
    result, _ = _run(data, cand, start, end)
    result = {**result, "final_equity": result["final_equity"] + 1234.5}
    rep = reconcile_backtest(result, initial_capital=CAPITAL,
                             rules={**RULES_DICT, "max_positions": 2}, bars=data)
    assert rep["consistent"] is False
    assert any("现金重放" in name for name in rep["failed_checks"])


def test_reconcile_flags_fifo_stat_mismatch():
    data, cand, start, end = _scenario()
    result, _ = _run(data, cand, start, end)
    result = {**result, "win_rate": 0.1234}
    rep = reconcile_backtest(result, initial_capital=CAPITAL,
                             rules={**RULES_DICT, "max_positions": 2}, bars=data)
    assert rep["consistent"] is False
    assert any("FIFO" in name for name in rep["failed_checks"])


def test_progress_cb_reports_each_trading_day():
    data, cand, start, end = _scenario()
    seen: list[tuple[int, int]] = []
    result, out = _run(data, cand, start, end, cb=lambda d, t: seen.append((d, t)))
    total = len(out["equity_curve"])
    assert total > 0
    assert len(seen) == total  # 逐交易日回调一次
    assert seen[-1] == (total - 1, total)
    assert all(t == total for _, t in seen)


def test_reconcile_script_runs_and_passes(tmp_path):
    """自检脚本端到端（合成样本）：退出码 0 + 报告落盘。"""
    out_file = tmp_path / "reconcile.md"
    proc = subprocess.run(
        [sys.executable, "scripts/reconcile_backtest.py", "--out", str(out_file)],
        cwd=str(BACKEND), capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(BACKEND), "PYTHONUTF8": "1"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = out_file.read_text(encoding="utf-8")
    assert "对账通过 ✅" in text
    assert "独立现金重放" in text and "涨跌停合规" in text