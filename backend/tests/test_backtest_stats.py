"""T5.7 验收：回测统计口径（累计收益/胜率/盈亏比/最大回撤/交易明细 FIFO 手算对账）。"""
from app.services.backtest_stats import compute_stats, fifo_pnl


def _t(code, side, d, price, qty, fee=0.0):
    return {"code": code, "side": side, "date": d, "price": price,
            "quantity": qty, "fee": fee}


# ---------- FIFO 单笔盈亏（含费用） ----------

def test_fifo_pnl_hand_calc_with_fees():
    trades = [
        _t("A", "buy", "2026-11-02", 10.0, 100, fee=1.0),
        _t("A", "sell", "2026-11-05", 12.0, 100, fee=1.2),
    ]
    details = fifo_pnl(trades)
    sell = details[0]
    # 盈亏 = 100×12 − 1.2 − (100×10 + 1) = 197.8
    assert abs(sell["pnl"] - 197.8) < 1e-6
    assert sell["buy_dates"] == ["2026-11-02"] and sell["cost"] == 1001.0


def test_fifo_partial_match_two_lots():
    trades = [
        _t("A", "buy", "d1", 10.0, 100, fee=1.0),
        _t("A", "buy", "d2", 11.0, 100, fee=1.1),
        _t("A", "sell", "d3", 12.0, 150, fee=1.8),
    ]
    details = fifo_pnl(trades)
    sell = details[0]
    # 成本 = 100×10 + 50×11 = 1550；买费摊销 = 1.0 + 0.55 = 1.55
    # 盈亏 = 150×12 − 1.8 − 1550 − 1.55 = 246.65
    assert abs(sell["pnl"] - 246.65) < 1e-6
    assert sell["buy_dates"] == ["d1", "d2"]


# ---------- 统计指标（手算） ----------

def test_stats_hand_calc():
    trades = [
        _t("A", "buy", "d1", 10.0, 100, fee=1.0),
        _t("A", "sell", "d2", 12.0, 100, fee=1.2),   # +197.8
        _t("B", "buy", "d3", 20.0, 50, fee=0.5),
        _t("B", "sell", "d4", 18.0, 50, fee=0.45),   # −100.95
    ]
    curve = [{"date": f"d{i}", "equity": eq} for i, eq in
             enumerate([100.0, 120.0, 90.0, 110.0])]
    stats = compute_stats(trades, curve, initial_capital=100.0)

    assert stats["closed_trades"] == 2
    assert stats["win_rate"] == 0.5                  # 1 胜 / 2 平仓
    assert abs(stats["profit_loss_ratio"] - 197.8 / 100.95) < 1e-3
    assert abs(stats["max_drawdown"] - 0.25) < 1e-6  # 1 − 90/120
    assert abs(stats["cumulative_return"] - 0.1) < 1e-9  # (110−100)/100
    pnl = [d["pnl"] for d in stats["trade_details"]]
    assert abs(pnl[0] - 197.8) < 1e-6 and abs(pnl[1] + 100.95) < 1e-6


def test_stats_edge_cases():
    # 无交易 / 只有买入 / 单边盈利无亏损
    empty = compute_stats([], [], initial_capital=10.0)
    assert empty["win_rate"] is None and empty["profit_loss_ratio"] is None
    assert empty["max_drawdown"] is None and empty["cumulative_return"] == 0.0

    only_buy = compute_stats([_t("A", "buy", "d1", 10.0, 1)], [],
                             initial_capital=10.0)
    assert only_buy["closed_trades"] == 0 and only_buy["win_rate"] is None

    wins_only = compute_stats(
        [_t("A", "buy", "d1", 1.0, 10), _t("A", "sell", "d2", 2.0, 10)],
        [{"date": "d1", "equity": 10.0}, {"date": "d2", "equity": 20.0}],
        initial_capital=10.0)
    assert wins_only["win_rate"] == 1.0
    assert wins_only["profit_loss_ratio"] is None   # 无亏损 → None
