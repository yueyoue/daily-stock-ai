"""T5.8 回测对账脚本：抽样 3 只标的手工推演 × 引擎结果交叉比对（开发自检）。

用**另一套实现**（app/services/backtest_reconcile.py）独立复算逐笔成交价/手续费/
涨跌停合规/T+1/FIFO 统计/逐日现金重放，与引擎（backtest_engine + backtest_stats）
产出比对，输出 Markdown 对账报告（可直接粘贴进 docs/开发进度文档.md T5.8 备注）。

用法（backend/ 目录下）：
    python scripts/reconcile_backtest.py                      # 合成样本（确定性，可进 CI）
    python scripts/reconcile_backtest.py --out reconcile.md   # 报告写入文件
    python scripts/reconcile_backtest.py --db --codes 600000,000001,300750
                                                              # 业务库真实数据（需 DATABASE_URL 可连）

退出码：对账通过 0 / 未通过 1（便于 CI 或人工脚本把门）。

注意事项：服务函数命名避开 test_ 前缀（pytest 误收集教训，见 T1.3 备注）。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/ 入口

from app.services.backtest_engine import TradeRules, simulate  # noqa: E402
from app.services.backtest_reconcile import reconcile_backtest  # noqa: E402
from app.services.backtest_stats import compute_stats  # noqa: E402

INITIAL_CAPITAL = 100000.0

# 合成样本场景参数（确定性，覆盖 T+1 / 涨停不买 / 跌停不卖 / 停牌顺延 / 调仓卖出）
SYNTH_DAYS = 40
SYNTH_CODES = ("600001", "600002", "600003")


def _business_days(start: date, n: int) -> list[date]:
    """自 start 起 n 个工作日（跳过周六日；合成样本用）。"""
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _mk_series(dates: list[date], base: float, adj_factor: float = 1.0, *,
               missing: tuple[int, ...] = (), limit_up: tuple[int, ...] = (),
               limit_down: tuple[int, ...] = (), drift: float = 1.004) -> list[dict]:
    """构造单标的日线序列；missing=当日无 K 线（停牌）；limit_up/limit_down=开盘一字板。

    复权因子恒定（adj_* = raw × adj_factor），用于验证"成交用后复权价、涨跌停用不复权价"口径。
    """
    rows: list[dict] = []
    last_close: float | None = None
    price = base
    for i, d in enumerate(dates):
        if i in missing:
            continue
        if i in limit_up and last_close is not None:
            o = c = round(last_close * 1.10, 2)
        elif i in limit_down and last_close is not None:
            o = c = round(last_close * 0.90, 2)
        else:
            o = round(price, 2)
            c = round(price * drift, 2)
            price = c
        h = round(max(o, c) * 1.005, 2)
        lo = round(min(o, c) * 0.995, 2)
        last_close = c
        rows.append({
            "date": d, "open": o, "high": h, "low": lo, "close": c,
            "volume": 1_000_000.0, "amount": c * 1_000_000.0,
            "adj_factor": adj_factor,
            "adj_open": o * adj_factor, "adj_high": h * adj_factor,
            "adj_low": lo * adj_factor, "adj_close": c * adj_factor,
        })
    return rows


def _synth_universe(dates: list[date]) -> tuple[dict[str, list[dict]], callable]:
    """合成 3 只标的 + 轮动候选函数（确定性，覆盖各撮合分支）。"""
    data = {
        "600001": _mk_series(dates, 10.0, 1.0, drift=1.005),
        # 600002：i=13 涨停（买单顺延）、i=21 跌停（卖单顺延）；复权因子 1.5（验证复权口径）
        "600002": _mk_series(dates, 20.0, 1.5, limit_up=(13,), limit_down=(21,), drift=1.003),
        # 600003：i=21（买单顺延）、i=27（卖单顺延）停牌；复权因子 2.0
        "600003": _mk_series(dates, 8.0, 2.0, missing=(21, 27), drift=1.006),
    }

    def candidate_fn(day: date) -> list[str]:
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

    return data, candidate_fn


def _db_universe(codes: list[str], start: date, end: date):
    """业务库真实数据取数（--db 模式）。"""
    from app.models.base import SessionLocal
    from app.services.backtest_data import load_universe_series
    from app.services.backtest_signals import build_candidate_fn

    db = SessionLocal()
    try:
        data, skipped = load_universe_series(db, start, end, codes=codes)
        if data:
            candidate_fn = build_candidate_fn(
                db, {"logic": "and", "conditions": [{"factor": "ma5", "op": ">", "value": 0.0}]},
                data)
        else:
            candidate_fn = lambda _d: []  # noqa: E731
        return data, candidate_fn, skipped
    finally:
        db.close()


def _run(data, candidate_fn, start: date, end: date, rules: TradeRules) -> tuple[dict, dict]:
    """跑引擎 + 统计，返回 (合并结果, 引擎原始输出)。"""
    calls = [0]

    def progress_cb(done: int, total: int) -> None:  # 顺带验证进度回调（T5.8）
        calls[0] = max(calls[0], total)

    out = simulate(data, candidate_fn, start, end, INITIAL_CAPITAL, rules,
                   progress_cb=progress_cb)
    stats = compute_stats(out["trades"], out["equity_curve"], INITIAL_CAPITAL)
    result = {**out, **stats}
    result["_progress_total"] = calls[0]
    return result, out


def _coverage(out: dict) -> dict[str, int]:
    """撮合规则触达统计（事件类型计数）。"""
    cov: dict[str, int] = {}
    for e in out.get("events") or []:
        cov[e["type"]] = cov.get(e["type"], 0) + 1
    return cov


def _render(report: dict, out: dict, *, mode: str, rules: TradeRules,
            span: str, data: dict, skipped: list[str]) -> str:
    cov = _coverage(out)
    trades = out["trades"]
    per_code: dict[str, dict] = {}
    for t in trades:
        s = per_code.setdefault(t["code"], {"buy": 0, "sell": 0, "days": 0})
        s[t["side"]] += 1
    for code, series in data.items():
        per_code.setdefault(code, {"buy": 0, "sell": 0, "days": 0})["days"] = len(series)
    factor_of = {code: (series[0]["adj_factor"] if series else None)
                 for code, series in data.items()}

    lines = [
        "# T5.8 回测对账报告（抽样 3 只标的手工推演 × 引擎）",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 取数模式：{mode}",
        f"- 区间：{span}",
        f"- 规则：滑点 {rules.slippage:.4%} / 手续费 {rules.fee_rate:.4%} / "
        f"涨跌停 ±{rules.limit_pct:.0%} / 最大持仓 {rules.max_positions} 只",
        f"- 初始资金：{INITIAL_CAPITAL:,.0f} 元；交易日 {report['equity_days']} 天；"
        f"成交 {report['trades']} 笔；期末资产 {report['final_equity']:,.2f} 元",
    ]
    if skipped:
        lines.append(f"- 跳过标的（复权因子缺失）：{skipped}")

    lines += ["", "## 标的抽样", "",
              "| 标的 | 复权因子 | 交易日 | 买入笔 | 卖出笔 |", "|---|---|---|---|---|"]
    for code in sorted(per_code):
        s = per_code[code]
        f = factor_of.get(code)
        lines.append(f"| {code} | {f if f is not None else '—'} | {s['days']} | "
                     f"{s['buy']} | {s['sell']} |")

    lines += ["", "## 撮合规则触达（引擎事件留痕）", "", "| 事件类型 | 次数 | 含义 |", "|---|---|---|"]
    meaning = {
        "buy_deferred_limit": "开盘涨停 → 买单顺延（涨停不买）",
        "sell_deferred_limit": "开盘跌停 → 卖单顺延（跌停不卖）",
        "buy_deferred_suspend": "停牌 → 买单顺延",
        "sell_deferred_suspend": "停牌 → 卖单顺延",
        "buy_cancelled": "信号失效 → 买单取消",
        "buy_skipped_cash": "现金不足 → 放弃买入",
    }
    for k in sorted(cov):
        lines.append(f"| {k} | {cov[k]} | {meaning.get(k, '—')} |")
    if not cov:
        lines.append("| （无） | 0 | 本区间未触发顺延/取消分支 |")

    lines += ["", "## 逐项对账", "", "| # | 对账项 | 结论 | 说明 |", "|---|---|---|---|"]
    for i, c in enumerate(report["checks"], 1):
        lines.append(f"| {i} | {c['name']} | {'✅ 一致' if c['ok'] else '❌ 不一致'} | "
                     f"{c['detail']} |")

    fifo = report["fifo"]
    lines += [
        "", "## 独立复算摘要", "",
        f"- 独立 FIFO：平仓 {fifo['closed']} 笔（盈 {fifo['wins']} / 亏 {fifo['losses']}）、"
        f"胜率 {fifo['win_rate']}、盈亏比 {fifo['profit_loss_ratio']}",
        f"- 独立现金重放期末资产：{report['replay_equity']:,.2f} 元（引擎 {report['final_equity']:,.2f} 元）",
        f"- 进度回调总交易日：{report.get('_progress_total') or out.get('_progress_total') or '—'}",
        "",
        "## 结论",
        "",
        ("**对账通过 ✅**：逐笔成交、手续费、涨跌停合规、T+1、FIFO 统计、"
         "逐日现金重放与引擎产出完全一致。" if report["consistent"] else
         f"**对账未通过 ❌**：不一致项 {report['failed_checks']}"),
        "",
        "> 由 backend/scripts/reconcile_backtest.py 生成（独立复算实现见 "
        "backend/app/services/backtest_reconcile.py）；粘贴进开发进度文档 T5.8 备注。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="T5.8 回测对账（抽样 3 只标的）")
    parser.add_argument("--db", action="store_true", help="业务库真实数据（默认合成样本）")
    parser.add_argument("--codes", default=None, help="--db 模式下的标的，逗号分隔（默认取库内 3 只）")
    parser.add_argument("--start", default=None, help="区间起 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="区间止 YYYY-MM-DD")
    parser.add_argument("--max-positions", type=int, default=2, help="最大持仓数（默认 2）")
    parser.add_argument("--out", default=None, help="报告写入文件（默认打印 stdout）")
    args = parser.parse_args()

    rules = TradeRules(slippage=0.001, fee_rate=0.0005, limit_pct=0.10,
                       max_positions=max(1, args.max_positions))
    skipped: list[str] = []

    if args.db:
        end = date.fromisoformat(args.end) if args.end else date.today()
        start = date.fromisoformat(args.start) if args.start else end - timedelta(days=365)
        codes = [c.strip() for c in args.codes.split(",")] if args.codes else None
        if codes is None:
            from app.models.base import SessionLocal
            from app.services.backtest_data import iter_universe

            db = SessionLocal()
            try:
                codes = iter_universe(db)[:3]
            finally:
                db.close()
            if not codes:
                raise SystemExit("业务库无 K 线数据，无法对账（或改用默认合成样本）")
        data, candidate_fn, skipped = _db_universe(codes, start, end)
        if not data:
            raise SystemExit("指定标的均无可用数据，无法对账")
        mode = f"业务库真实数据（{','.join(sorted(data))}）"
    else:
        dates = _business_days(date(2025, 1, 2), SYNTH_DAYS)
        data, candidate_fn = _synth_universe(dates)
        start, end = dates[0], dates[-1]
        mode = "合成样本（确定性，覆盖 T+1/涨跌停/停牌/调仓）"

    result, out = _run(data, candidate_fn, start, end, rules)
    report = reconcile_backtest(result, initial_capital=INITIAL_CAPITAL,
                                rules={"slippage": rules.slippage,
                                       "fee_rate": rules.fee_rate,
                                       "limit_pct": rules.limit_pct}, bars=data)
    report["_progress_total"] = result.get("_progress_total")
    md = _render(report, out, mode=mode, rules=rules,
                 span=f"{start} ~ {end}", data=data, skipped=skipped)

    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"对账报告已写入 {args.out}（{'通过' if report['consistent'] else '未通过'}）")
    else:
        print(md)
    return 0 if report["consistent"] else 1


if __name__ == "__main__":
    sys.exit(main())