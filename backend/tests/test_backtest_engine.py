"""T5.6 验收：回测撮合与组合（T+1 / 涨停不买 / 跌停不卖 / 停牌顺延 / 滑点费用 /
信号失效取消 / 等权分仓 / 逐笔可追溯 + 策略历史回放信号源）。

场景用例逐条对照 backtest_engine 模块注释中的撮合规则。
"""
from datetime import date, timedelta

import pytest

from app.core.errors import AppError
from app.models.base import SessionLocal, init_db
from app.models.market import StockFinIndicator
from app.services.backtest_engine import TradeRules, simulate
from app.services.backtest_signals import build_candidate_fn

D1 = date(2026, 11, 2)
DAYS = [D1 + timedelta(days=i) for i in range(5)]
D2, D3, D4, D5 = DAYS[1], DAYS[2], DAYS[3], DAYS[4]


def setup_function():
    init_db()


def _bar(d, o, c, adj=1.0):
    return {"date": d, "open": o, "high": max(o, c), "low": min(o, c), "close": c,
            "volume": 1000.0, "amount": 1000.0 * c, "adj_factor": adj,
            "adj_open": o * adj, "adj_high": max(o, c) * adj,
            "adj_low": min(o, c) * adj, "adj_close": c * adj}


def _fn(mapping):
    return lambda d: mapping.get(d, [])


def _rules(**kw):
    base = {"slippage": 0.0, "fee_rate": 0.0, "max_positions": 1}
    base.update(kw)
    return TradeRules(**base)


# ---------- 规则 1：T+1 开盘成交 + 逐笔可追溯 + 资金曲线 ----------

def test_t1_open_fill_traceable_and_equity_curve():
    data = {"A": [_bar(D1, 10, 10), _bar(D2, 10.5, 12), _bar(D3, 12, 12)]}
    out = simulate(data, _fn({D1: ["A"], D2: ["A"]}), D1, D3,
                   initial_capital=1000, rules=_rules())

    assert len(out["trades"]) == 1
    t = out["trades"][0]
    assert t["side"] == "buy" and t["date"] == D2.isoformat()
    assert t["signal_date"] == D1.isoformat()          # T+1：信号 D1 → 成交 D2
    assert t["price"] == 10.5 and t["quantity"] == 95  # 1000/10.5 向下取整
    assert t["reason"] == "入选目标持仓" and t["fee"] == 0

    assert [p["equity"] for p in out["equity_curve"]] == [1000.0, 1142.5, 1142.5]
    assert [p["cash"] for p in out["equity_curve"]] == [1000.0, 2.5, 2.5]
    assert out["final_equity"] == 1142.5


# ---------- 规则 2：涨停不买入（顺延 / 信号失效取消） ----------

def test_limit_up_buy_deferred_then_cancelled_or_filled():
    data = {"A": [_bar(D1, 10, 10), _bar(D2, 11, 11), _bar(D3, 11.5, 12)]}

    # ① D2 开盘=前收 10×1.1 涨停 → 不买顺延；D2 信号消失 → D3 取消
    out = simulate(data, _fn({D1: ["A"], D2: []}), D1, D3,
                   initial_capital=1000, rules=_rules())
    kinds = [(e["date"], e["type"]) for e in out["events"]]
    assert (D2.isoformat(), "buy_deferred_limit") in kinds
    assert (D3.isoformat(), "buy_cancelled") in kinds
    assert out["trades"] == []

    # ② 信号持续 → D3 开盘未涨停成交（顺延生效）
    out2 = simulate(data, _fn({D1: ["A"], D2: ["A"]}), D1, D3,
                    initial_capital=1000, rules=_rules())
    assert [t["date"] for t in out2["trades"]] == [D3.isoformat()]
    assert out2["trades"][0]["price"] == 11.5


# ---------- 规则 3：跌停不卖出（顺延） ----------

def test_limit_down_sell_deferred():
    data = {"A": [_bar(D1, 10, 10), _bar(D2, 10, 10), _bar(D3, 20, 20),
                  _bar(D4, 18, 18.5), _bar(D5, 19, 19)]}
    out = simulate(data, _fn({D1: ["A"], D2: ["A"], D3: []}), D1, D5,
                   initial_capital=1000, rules=_rules())

    kinds = [(e["date"], e["type"]) for e in out["events"]]
    assert (D4.isoformat(), "sell_deferred_limit") in kinds  # 开盘 18=前收20×0.9 跌停
    sides = [(t["side"], t["date"]) for t in out["trades"]]
    assert sides == [("buy", D2.isoformat()), ("sell", D5.isoformat())]
    sell = out["trades"][1]
    assert sell["signal_date"] == D3.isoformat() and sell["price"] == 19
    assert sell["reason"] == "调出目标持仓"
    assert out["final_equity"] == 1900.0  # 100 股 × 19


# ---------- 规则 4：停牌跳过（买/卖顺延） ----------

def test_suspension_defers_both_sides():
    # A 缺 D2/D4（停牌）；B 全程交易撑起日历
    data = {
        "A": [_bar(D1, 10, 10), _bar(D3, 10.5, 11), _bar(D5, 11, 12)],
        "B": [_bar(d, 5, 5) for d in DAYS],
    }
    out = simulate(data, _fn({D1: ["A"], D2: ["A"], D3: []}), D1, D5,
                   initial_capital=1000, rules=_rules(max_positions=2))

    kinds = [(e["date"], e["type"]) for e in out["events"]]
    assert (D2.isoformat(), "buy_deferred_suspend") in kinds
    assert (D4.isoformat(), "sell_deferred_suspend") in kinds
    sides = [(t["code"], t["side"], t["date"]) for t in out["trades"]]
    assert sides == [("A", "buy", D3.isoformat()), ("A", "sell", D5.isoformat())]


# ---------- 规则 9：等权分仓 ----------

def test_equal_weight_positions():
    data = {"A": [_bar(D1, 10, 10), _bar(D2, 10, 10)],
            "B": [_bar(D1, 20, 20), _bar(D2, 20, 20)]}
    out = simulate(data, _fn({D1: ["A", "B"]}), D1, D2,
                   initial_capital=1000, rules=_rules(max_positions=2))
    qty = {t["code"]: t["quantity"] for t in out["trades"]}
    assert qty == {"A": 50, "B": 25}  # 各 500 元目标仓位


# ---------- 规则 6/7：滑点与手续费（手算） ----------

def test_slippage_and_fee_hand_calc():
    data = {"A": [_bar(D1, 10, 10), _bar(D2, 10, 10), _bar(D3, 11, 11)]}
    out = simulate(data, _fn({D1: ["A"], D2: []}), D1, D3,
                   initial_capital=1000,
                   rules=TradeRules(slippage=0.001, fee_rate=0.0005,
                                    max_positions=1))
    buy, sell = out["trades"]
    assert abs(buy["price"] - 10 * 1.001) < 1e-9       # 买价=开盘×(1+滑点)
    assert buy["quantity"] == 99                        # 1000/10.01 取整
    assert abs(buy["fee"] - 99 * 10.01 * 0.0005) < 1e-5
    assert abs(sell["price"] - 11 * 0.999) < 1e-9       # 卖价=开盘×(1-滑点)
    assert abs(sell["fee"] - 99 * 11 * 0.999 * 0.0005) < 1e-5
    expect_final = 1000 - 99 * 10.01 * 1.0005 + 99 * 11 * 0.999 * (1 - 0.0005)
    assert abs(out["final_equity"] - expect_final) < 1e-3  # 金额四舍五入到 4 位


# ---------- 信号源：策略历史回放 ----------

def test_strategy_replay_candidates_and_fin_carry_forward():
    db = SessionLocal()
    try:
        for model in (StockFinIndicator,):
            db.query(model).delete()
        db.commit()

        # A 一路上涨、B 一路下跌（各 12 根）
        data = {
            "A": [_bar(D1 + timedelta(days=i), 10 + i, 10 + i) for i in range(12)],
            "B": [_bar(D1 + timedelta(days=i), 20 - i, 20 - i) for i in range(12)],
        }
        fn = build_candidate_fn(db, {
            "logic": "and",
            "conditions": [{"factor": "ma5", "op": ">=", "value": 15.0}],
        }, data)
        last_day = D1 + timedelta(days=11)
        assert fn(D1) == []                  # 首日 ma5 未定义 → 不命中
        assert fn(last_day) == ["A"]         # 末日 A ma5=19≥15；B ma5=11<15

        # 财务因子 carry-forward（报告期后生效，之前不命中）
        db.add(StockFinIndicator(code="B", report_date=D1 + timedelta(days=5),
                                 metrics={"roe": 20.0}))
        db.commit()
        fn2 = build_candidate_fn(db, {
            "logic": "and",
            "conditions": [{"factor": "roe", "op": ">=", "value": 15.0}],
        }, data)
        assert fn2(D1) == []                                     # 报告期前无值
        assert fn2(D1 + timedelta(days=5)) == ["B"]              # 报告期起生效

        # 清单外自定义参数因子 → 明确 400
        with pytest.raises(AppError) as ei:
            build_candidate_fn(db, {"logic": "and", "conditions": [
                {"factor": "ma30", "op": ">", "value": 1}]}, data)
        assert ei.value.code == 400
    finally:
        db.close()
