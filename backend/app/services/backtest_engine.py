"""回测引擎 — 撮合与组合（T5.6，开发文档 §4 模块5 口径四件套之 ③撮合规则 ④滑点）。

**每日轮动组合模型**（开发文档未定退出规则，口径决策记录于进度文档备注）：
每个交易日 D 收盘按信号生成目标持仓（候选前 max_positions 名，等权分仓），
次一交易日开盘执行调仓；调出目标的持仓**全部卖出**，跌出候选即离场。

撮合规则（pytest 场景用例逐条把关）：
1. **T+1**：信号日 D 收盘生成目标 → 次一交易日开盘价成交
2. **涨停不买入**：执行日开盘价 ≥ 该股前收涨停价 → 当日不买，顺延次日再试
3. **跌停不卖出**：执行日开盘价 ≤ 该股前收跌停价 → 当日不卖，顺延次日再试
4. **停牌跳过**：执行日该股无 K 线 → 顺延（买/卖一致）
5. **信号失效**：买单顺延执行时若标的已不在最近一期目标持仓 → 取消留痕
   （卖单不取消——调出即必须离场，停牌/跌停只顺延）
6. **单边滑点** slippage（默认 0.1%）：买价=后复权开盘×(1+s)，卖价=后复权开盘×(1-s)
7. **手续费** fee_rate（默认 0.05%，含税费，买卖双边）：费=成交额×fee_rate
8. 涨跌停判定用**不复权价**（交易所规则作用于真实报价，前收=该股自身最后成交日收盘）；
   涨跌停价 = 前收×(1±limit_pct) **四舍五入到分**（交易所口径，兼进实盘撮合取整）；
   成交与盈亏用**后复权价**（除权不失真，与 T5.5 口径一致）
9. 数量按目标金额/成交价向下取整股（不强制整手，简化口径）；单票目标资产=总资产/max_positions
10. 逐笔记录可追溯：signal_date（信号日）/ date（成交日）/ reason / price / fee 全留
11. **进度回传**（T5.8）：simulate(..., progress_cb) 逐交易日回调 (done, total)，长周期回测可查进度

输出：trades（逐笔）、equity_curve（按日 mark-to-market 后复权收盘）、events
（顺延/取消/涨停跌停跳过留痕）、final_equity、open_orders（期末未成交挂单）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

logger = logging.getLogger("app.backtest_engine")


@dataclass
class TradeRules:
    slippage: float = 0.001      # 单边滑点（0.1%）
    fee_rate: float = 0.0005     # 手续费含税费（0.05%），双边
    limit_pct: float = 0.10      # 涨跌停幅度（简化口径：非 ST 一律 ±10%，可配）
    max_positions: int = 5       # 最大持仓数（等权分仓）


@dataclass
class _Order:
    code: str
    signal_date: date


def simulate(data: dict[str, list[dict]], candidate_fn, start: date, end: date,
             initial_capital: float = 100000.0,
             rules: TradeRules | None = None,
             progress_cb=None) -> dict:
    """执行回测模拟，返回 trades / equity_curve / events / final_equity / open_orders。

    data: {code: 序列}（backtest_data.load_series 输出：含不复权与后复权字段）
    candidate_fn: (date) -> list[str] 有序候选（信号源，如 backtest_signals）
    progress_cb: 可选 (done, total) 进度回调（T5.8 异步可查进度，逐交易日触发）
    """
    rules = rules or TradeRules()
    rules.max_positions = max(1, int(rules.max_positions))

    bars: dict[str, dict[date, dict]] = {}
    for code, series in (data or {}).items():
        hist = {r["date"]: r for r in series if start <= r["date"] <= end}
        if hist:
            bars[code] = hist
    calendar = sorted({d for hist in bars.values() for d in hist})
    # 每股前收（自身最后成交日收盘，涨跌停判定用；预计算）
    prev_closes: dict[str, dict[date, float | None]] = {}
    for code, hist in bars.items():
        ds = sorted(hist)
        prev_closes[code] = {
            ds[i]: (hist[ds[i - 1]]["close"] if i > 0 else None)
            for i in range(len(ds))
        }

    cash = float(initial_capital)
    holdings: dict[str, int] = {}
    last_adj_close: dict[str, float] = {}
    last_equity = float(initial_capital)
    pending_buys: dict[str, _Order] = {}
    pending_sells: dict[str, _Order] = {}
    latest_targets: set[str] = set()
    trades: list[dict] = []
    events: list[dict] = []
    equity_curve: list[dict] = []

    def _event(d, code, kind, detail):
        events.append({"date": d.isoformat(), "code": code,
"type": kind, "detail": detail})

    for i, d in enumerate(calendar):
        if progress_cb is not None:
            progress_cb(i, len(calendar))  # T5.8 进度回传（已处理交易日/总交易日）
        # ---------- 开盘撮合：先卖后买（T+1/顺延订单） ----------
        for code in list(pending_sells):
            order = pending_sells[code]
            if code not in bars or d not in bars[code]:
                _event(d, code, "sell_deferred_suspend", "停牌，卖出顺延")
                continue
            row = bars[code][d]
            pc = prev_closes[code].get(d)
            if pc is not None and row["open"] <= round(pc * (1 - rules.limit_pct), 2):
                _event(d, code, "sell_deferred_limit", "开盘跌停，卖出顺延")
                continue
            qty = holdings.pop(code, 0)
            if qty <= 0:
                pending_sells.pop(code, None)
                continue
            fill = row["adj_open"] * (1 - rules.slippage)
            fee = qty * fill * rules.fee_rate
            cash += qty * fill - fee
            trades.append({
                "code": code, "side": "sell", "date": d.isoformat(),
                "signal_date": order.signal_date.isoformat(),
                "price": round(fill, 6), "raw_open": row["open"],
                "quantity": qty, "fee": round(fee, 6),
                "amount": round(qty * fill, 6), "reason": "调出目标持仓",
            })
            last_adj_close.pop(code, None)
            pending_sells.pop(code, None)

        for code in list(pending_buys):
            order = pending_buys[code]
            if code not in latest_targets:
                _event(d, code, "buy_cancelled",
                       f"信号失效（{order.signal_date} 入选，已不在目标持仓）")
                pending_buys.pop(code, None)
                continue
            if code not in bars or d not in bars[code]:
                _event(d, code, "buy_deferred_suspend", "停牌，买入顺延")
                continue
            row = bars[code][d]
            pc = prev_closes[code].get(d)
            if pc is not None and row["open"] >= round(pc * (1 + rules.limit_pct), 2):
                _event(d, code, "buy_deferred_limit", "开盘涨停，买入顺延")
                continue
            fill = row["adj_open"] * (1 + rules.slippage)
            slot = last_equity / rules.max_positions
            budget = min(cash, slot)
            qty = int(budget / fill) if fill > 0 else 0
            if qty > 0 and qty * fill * (1 + rules.fee_rate) > cash:
                qty = int(cash / (fill * (1 + rules.fee_rate)))
            if qty <= 0:
                _event(d, code, "buy_skipped_cash", "现金不足，放弃买入")
                pending_buys.pop(code, None)
                continue
            fee = qty * fill * rules.fee_rate
            cash -= qty * fill + fee
            holdings[code] = holdings.get(code, 0) + qty
            trades.append({
                "code": code, "side": "buy", "date": d.isoformat(),
                "signal_date": order.signal_date.isoformat(),
                "price": round(fill, 6), "raw_open": row["open"],
                "quantity": qty, "fee": round(fee, 6),
                "amount": round(qty * fill, 6), "reason": "入选目标持仓",
            })
            pending_buys.pop(code, None)

        # ---------- 收盘 mark-to-market ----------
        market_value = 0.0
        for code, qty in holdings.items():
            row = bars[code].get(d)
            if row is not None:
                last_adj_close[code] = row["adj_close"]
            market_value += qty * last_adj_close.get(code, 0.0)
        equity = cash + market_value
        last_equity = equity
        equity_curve.append({
            "date": d.isoformat(), "equity": round(equity, 4),
            "cash": round(cash, 4), "market_value": round(market_value, 4),
        })

        # ---------- 收盘生成新信号（次日开盘执行） ----------
        if d == calendar[-1] or not (start <= d <= end):
            continue
        targets = [c for c in (candidate_fn(d) or []) if c in bars]
        latest_targets = set(targets[:rules.max_positions])
        for code in sorted(latest_targets):
            if code not in holdings and code not in pending_buys:
                pending_buys[code] = _Order(code, d)
        for code in list(holdings):
            if code not in latest_targets and code not in pending_sells:
                pending_sells[code] = _Order(code, d)

    open_orders = {
        "buys": [{"code": o.code, "signal_date": o.signal_date.isoformat()}
                 for o in pending_buys.values()],
        "sells": [{"code": o.code, "signal_date": o.signal_date.isoformat()}
                  for o in pending_sells.values()],
    }
    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "events": events,
        "final_equity": round(last_equity, 4),
        "open_orders": open_orders,
    }
