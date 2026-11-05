"""回测统计（T5.7，开发文档 §4 模块5 输出）。

口径（手算抽样 pytest 把关）：
- **累计收益率** cumulative_return = (final_equity − initial_capital) / initial_capital（小数）
- **胜率** win_rate = 盈利平仓笔数 / 平仓笔数（FIFO 配对，0 盈亏计入分母不计分子；小数）
- **盈亏比** profit_loss_ratio = 平均单笔盈利 / |平均单笔亏损|（无盈利或无亏损 → None）
- **最大回撤** max_drawdown = max(1 − equity/cummax_equity)（小数幅度，0.184 = 18.4%）
- **交易明细** trade_details：卖出笔 FIFO 配对买入成本 → 单笔盈亏（含费用，可追溯）

单位约定：win_rate / max_drawdown / cumulative_return 为小数（前端 ×100 展示为 %）。
"""
from __future__ import annotations

from collections import deque


def fifo_pnl(trades: list[dict]) -> list[dict]:
    """按标的 FIFO 配对，为每笔卖出算单笔盈亏（含买卖费用摊销）。"""
    books: dict[str, deque] = {}
    out: list[dict] = []
    for t in trades:
        code, side = t.get("code"), t.get("side")
        qty = int(t.get("quantity") or 0)
        if side == "buy":
            fee = t.get("fee") or 0.0
            books.setdefault(code, deque()).append({
                "qty": qty, "price": t.get("price") or 0.0,
                "unit_fee": fee / qty if qty else 0.0, "date": t.get("date")})
            continue
        if side != "sell" or qty <= 0:
            continue
        remaining = qty
        total_cost = 0.0
        total_buy_fee = 0.0
        matched_dates: list[str] = []
        book = books.get(code, deque())
        while remaining > 0 and book:
            lot = book[0]
            take = min(remaining, lot["qty"])
            total_cost += take * lot["price"]
            total_buy_fee += take * lot["unit_fee"]
            matched_dates.append(lot["date"])
            lot["qty"] -= take
            remaining -= take
            if lot["qty"] <= 0:
                book.popleft()
        proceeds = qty * (t.get("price") or 0.0)
        sell_fee = t.get("fee") or 0.0
        pnl = proceeds - sell_fee - total_cost - total_buy_fee
        out.append({**t, "pnl": round(pnl, 4),
                    "buy_dates": matched_dates,
                    "cost": round(total_cost + total_buy_fee, 4)})
    return out


def compute_stats(trades: list[dict], equity_curve: list[dict],
                  initial_capital: float) -> dict:
    """统计指标 + 交易明细（trade_details）。"""
    details = fifo_pnl(trades or [])
    closed = [d for d in details if d.get("side") == "sell"]
    wins = [d for d in closed if (d.get("pnl") or 0) > 0]
    losses = [d for d in closed if (d.get("pnl") or 0) < 0]

    win_rate = (len(wins) / len(closed)) if closed else None
    if wins and losses:
        avg_w = sum(d["pnl"] for d in wins) / len(wins)
        avg_l = sum(d["pnl"] for d in losses) / len(losses)
        profit_loss_ratio = round(avg_w / abs(avg_l), 4) if avg_l else None
    else:
        profit_loss_ratio = None

    max_dd = None
    if equity_curve:
        peak = None
        for p in equity_curve:
            eq = p.get("equity")
            if eq is None:
                continue
            peak = eq if peak is None else max(peak, eq)
            dd = 1 - eq / peak if peak else 0.0
            max_dd = dd if max_dd is None else max(max_dd, dd)

    final_equity = equity_curve[-1]["equity"] if equity_curve else initial_capital
    cumulative_return = (final_equity - initial_capital) / initial_capital \
        if initial_capital else None

    return {
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "profit_loss_ratio": profit_loss_ratio,
        "max_drawdown": round(max_dd, 4) if max_dd is not None else None,
        "cumulative_return": round(cumulative_return, 6)
        if cumulative_return is not None else None,
        "closed_trades": len(closed),
        "trade_details": details,
    }
