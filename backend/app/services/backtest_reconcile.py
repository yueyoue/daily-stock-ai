"""回测对账（T5.8）：独立复算引擎输出，抽样标的手工推演与引擎结果交叉比对。

**独立性**：本模块不调用 backtest_engine / backtest_stats 的任何内部函数，
仅凭引擎产出（trades / equity_curve / final_equity / 统计指标）+ 原始日线序列
+ 规则参数，用另一套实现重算每一笔成交与逐日现金，反向验证引擎无误。

对账项（任一失败 → consistent=False）：
1. **逐笔成交价**：price == adj_open × (1 ± slippage)（引擎四舍五入到 6 位）
2. **逐笔手续费**：fee == quantity × fill × fee_rate
3. **涨跌停合规**：买单成交日 raw_open < 前收×(1+limit_pct)；
   卖单 raw_open > 前收×(1−limit_pct)（否则引擎应顺延而非成交）
4. **T+1**：成交日 > 信号日
5. **独立 FIFO 复算**：平仓笔数 / 胜率 / 盈亏比 == 引擎统计值
6. **独立现金重放**：初始资金起按逐笔交易重放，期末现金 + 持仓市值 == final_equity

用法（见 backend/scripts/reconcile_backtest.py）：
    from app.services.backtest_reconcile import reconcile_backtest
    report = reconcile_backtest(result, initial_capital=100000.0,
                                rules={"slippage":0.001,"fee_rate":0.0005,
                                       "limit_pct":0.1,"max_positions":5},
                                bars=data)   # {code: [日线行, ...]}
"""
from __future__ import annotations

import logging

logger = logging.getLogger("app.backtest_reconcile")

PRICE_TOL = 1e-6      # 成交价四舍五入 6 位后的容差
FEE_TOL = 1e-6        # 手续费 6 位容差
EQUITY_TOL = 0.01     # 期末资产 4 位小数容差


def _iso(d) -> str:
    """date / datetime / 字符串 → 'YYYY-MM-DD'。"""
    return d.isoformat()[:10] if hasattr(d, "isoformat") else str(d)[:10]


def _index_bars(bars: dict[str, list[dict]]) -> tuple[dict[str, dict[str, dict]],
                                                     dict[str, dict[str, float]]]:
    """日线序列 → ({code: {iso_date: row}}, {code: {iso_date: 前收}})。

    前收口径与引擎一致：该股自身序列中的上一成交日收盘（不复权）。
    """
    indexed: dict[str, dict[str, dict]] = {}
    prev_close: dict[str, dict[str, float]] = {}
    for code, series in (bars or {}).items():
        by_date = {_iso(r["date"]): r for r in (series or [])}
        indexed[code] = by_date
        dates = sorted(by_date)
        prev_close[code] = {
            dates[i]: by_date[dates[i - 1]]["close"] for i in range(1, len(dates))
        }
    return indexed, prev_close


def _independent_fifo(trades: list[dict]) -> list[dict]:
    """独立实现的 FIFO 配对（与 backtest_stats 另一套写法），返回卖出笔盈亏明细。"""
    lots: dict[str, list[dict]] = {}
    closed: list[dict] = []
    for t in trades:
        code, side = t.get("code"), t.get("side")
        qty = int(t.get("quantity") or 0)
        if qty <= 0:
            continue
        if side == "buy":
            lots.setdefault(code, []).append({
                "qty": qty, "price": float(t.get("price") or 0.0),
                "fee_per_share": (float(t.get("fee") or 0.0) / qty),
            })
            continue
        if side != "sell":
            continue
        remaining = qty
        cost = 0.0
        buy_fee = 0.0
        book = lots.get(code, [])
        while remaining > 0 and book:
            lot = book[0]
            take = min(remaining, lot["qty"])
            cost += take * lot["price"]
            buy_fee += take * lot["fee_per_share"]
            lot["qty"] -= take
            remaining -= take
            if lot["qty"] <= 0:
                book.pop(0)
        pnl = qty * float(t.get("price") or 0.0) - float(t.get("fee") or 0.0) - cost - buy_fee
        closed.append({"code": code, "date": t.get("date"), "pnl": round(pnl, 4)})
    return closed


def reconcile_backtest(result: dict, *, initial_capital: float, rules: dict,
                       bars: dict[str, list[dict]]) -> dict:
    """独立复算并与引擎产出交叉比对，返回对账报告。

    result: 引擎输出（含 trades / equity_curve / final_equity / win_rate /
            profit_loss_ratio / closed_trades）
    bars: 与回测同区间的原始日线序列 {code: [row]}（row 含 date/open/adj_open/adj_close）
    """
    trades = list(result.get("trades") or [])
    equity_curve = list(result.get("equity_curve") or [])
    final_equity = float(result.get("final_equity") or 0.0)
    slippage = float(rules.get("slippage", 0.001))
    fee_rate = float(rules.get("fee_rate", 0.0005))
    limit_pct = float(rules.get("limit_pct", 0.10))

    indexed, prev_close = _index_bars(bars)
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    # ---- 1~4：逐笔（成交价 / 手续费 / 涨跌停合规 / T+1） ----
    price_bad: list[str] = []
    fee_bad: list[str] = []
    limit_bad: list[str] = []
    t1_bad: list[str] = []
    for idx, t in enumerate(trades):
        code = t.get("code")
        side = t.get("side")
        qty = int(t.get("quantity") or 0)
        fill = float(t.get("price") or 0.0)
        fee = float(t.get("fee") or 0.0)
        tag = f"#{idx + 1} {code} {side} {t.get('date')}"
        row = indexed.get(code, {}).get(_iso(t.get("date")))
        if row is None:
            price_bad.append(f"{tag} 无对应日线")
        else:
            adj_open = float(row["adj_open"])
            expect = adj_open * (1 + slippage if side == "buy" else 1 - slippage)
            if abs(fill - round(expect, 6)) > PRICE_TOL:
                price_bad.append(f"{tag} 价 {fill} ≠ 期望 {round(expect, 6)}")
            expect_fee = qty * adj_open * (1 + slippage if side == "buy" else 1 - slippage) * fee_rate
            if abs(fee - round(expect_fee, 6)) > max(FEE_TOL, abs(expect_fee) * 1e-9):
                fee_bad.append(f"{tag} 费 {fee} ≠ 期望 {round(expect_fee, 6)}")
            pc = prev_close.get(code, {}).get(_iso(t.get("date")))
            raw_open = float(t.get("raw_open", row.get("open")))
            if pc is not None:
                if side == "buy" and raw_open >= round(pc * (1 + limit_pct), 2):
                    limit_bad.append(f"{tag} 买单在涨停价 {raw_open} 成交（应顺延）")
                if side == "sell" and raw_open <= round(pc * (1 - limit_pct), 2):
                    limit_bad.append(f"{tag} 卖单在跌停价 {raw_open} 成交（应顺延）")
        sig, dt = t.get("signal_date"), t.get("date")
        if not sig or not dt or _iso(dt) <= _iso(sig):
            t1_bad.append(f"{tag} 成交日未晚于信号日（signal={sig}）")

    add("逐笔成交价（adj_open×(1±滑点)）", not price_bad,
        f"{len(trades)} 笔全部一致" if not price_bad else "; ".join(price_bad[:5]))
    add("逐笔手续费（qty×成交价×费率）", not fee_bad,
        f"{len(trades)} 笔全部一致" if not fee_bad else "; ".join(fee_bad[:5]))
    add("涨跌停合规（涨停不买 / 跌停不卖）", not limit_bad,
        "无越界成交" if not limit_bad else "; ".join(limit_bad[:5]))
    add("T+1（成交日晚于信号日）", not t1_bad,
        f"{len(trades)} 笔全部满足" if not t1_bad else "; ".join(t1_bad[:5]))

    # ---- 5：独立 FIFO 复算（平仓笔数 / 胜率 / 盈亏比） ----
    closed = _independent_fifo(trades)
    wins = [c for c in closed if c["pnl"] > 0]
    losses = [c for c in closed if c["pnl"] < 0]
    ind_win_rate = round(len(wins) / len(closed), 4) if closed else None
    if wins and losses:
        avg_w = sum(c["pnl"] for c in wins) / len(wins)
        avg_l = sum(c["pnl"] for c in losses) / len(losses)
        ind_plr = round(avg_w / abs(avg_l), 4)
    else:
        ind_plr = None
    eng_closed = result.get("closed_trades")
    eng_win = result.get("win_rate")
    eng_plr = result.get("profit_loss_ratio")
    fifo_bad = []
    if eng_closed is not None and int(eng_closed) != len(closed):
        fifo_bad.append(f"平仓笔数 引擎 {eng_closed} ≠ 独立 {len(closed)}")
    if eng_win is not None and ind_win_rate is not None and abs(float(eng_win) - ind_win_rate) > 1e-9:
        fifo_bad.append(f"胜率 引擎 {eng_win} ≠ 独立 {ind_win_rate}")
    if (eng_plr is None) != (ind_plr is None) or (
            eng_plr is not None and ind_plr is not None and abs(float(eng_plr) - ind_plr) > 1e-9):
        fifo_bad.append(f"盈亏比 引擎 {eng_plr} ≠ 独立 {ind_plr}")
    add("独立 FIFO 复算（平仓/胜率/盈亏比）", not fifo_bad,
        f"平仓 {len(closed)} 笔、胜率 {ind_win_rate}、盈亏比 {ind_plr} 与引擎一致"
        if not fifo_bad else "; ".join(fifo_bad))

    # ---- 6：独立现金重放（期末现金 + 持仓市值 == final_equity） ----
    cash = float(initial_capital)
    holding: dict[str, int] = {}
    for t in trades:
        code = t.get("code")
        qty = int(t.get("quantity") or 0)
        fill = float(t.get("price") or 0.0)
        fee = float(t.get("fee") or 0.0)
        if t.get("side") == "buy":
            cash -= qty * fill + fee
            holding[code] = holding.get(code, 0) + qty
        else:
            cash += qty * fill - fee
            holding[code] = holding.get(code, 0) - qty
    market_value = 0.0
    for code, qty in holding.items():
        if qty <= 0:
            continue
        series = sorted(indexed.get(code, {}))
        if series:
            market_value += qty * float(indexed[code][series[-1]]["adj_close"])
    replay_equity = cash + market_value
    ok_equity = abs(replay_equity - final_equity) <= EQUITY_TOL
    add("独立现金重放（期末现金+市值==期末资产）", ok_equity,
        f"重放 {round(replay_equity, 4)} vs 引擎 {round(final_equity, 4)}、"
        f"期末现金 {round(cash, 2)}、持仓市值 {round(market_value, 2)}")

    # ---- 汇总 ----
    failed = [c for c in checks if not c["ok"]]
    report = {
        "consistent": not failed,
        "trades": len(trades),
        "equity_days": len(equity_curve),
        "initial_capital": initial_capital,
        "final_equity": final_equity,
        "replay_equity": round(replay_equity, 4),
        "fifo": {"closed": len(closed), "wins": len(wins), "losses": len(losses),
                 "win_rate": ind_win_rate, "profit_loss_ratio": ind_plr},
        "checks": checks,
        "failed_checks": [c["name"] for c in failed],
    }
    if failed:
        logger.error("回测对账未通过：%s", report["failed_checks"])
    return report