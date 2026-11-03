"""T2.2 验收：技术指标 16 项双引擎（清单固化 / 口径手算 / 两引擎对账 / 配置切换）。

- 手算对账：按口径文档公式在测试侧独立实现，逐因子核对计算结果（防实现口径漂移）
- 两引擎对账：同一数据 talib vs pandas 全因子相对误差 <1e-6 且 None 对齐一致
  （有 TA-Lib 的环境执行；CI 装 TA-Lib 轮子即跑）
"""
import math
import random
import sys

import pytest

from app.core.config import get_settings
from app.services.tech_indicators import (
    FLAT_KEYS,
    INDICATOR_SPEC,
    PandasTechEngine,
    compute_indicators,
    compute_series,
    get_engine,
)

# ---------- 独立口径公式（与实现分离，来自 INDICATOR_SPEC 描述） ----------


def _f_ema(x, n):
    a = 2 / (n + 1)
    out = [None] * len(x)
    out[n - 1] = sum(x[:n]) / n
    for i in range(n, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def _f_ema_from(y, n):
    idx = [i for i, v in enumerate(y) if v is not None]
    seg = [y[i] for i in idx]
    tail = _f_ema(seg, n)
    out = [None] * len(y)
    for i, v in zip(idx, tail):
        out[i] = v
    return out


def _f_sma_last(x, n):
    return sum(x[-n:]) / n


def _series(n=60, seed=11):
    rng = random.Random(seed)
    c, h, l, v = [], [], [], []
    price = 10.0
    for _ in range(n):
        price *= 1 + rng.gauss(0.001, 0.02)
        c.append(price)
        h.append(price * (1 + abs(rng.gauss(0, 0.01))))
        l.append(price * (1 - abs(rng.gauss(0, 0.01))))
        v.append(rng.uniform(1e6, 5e6))
    return c, h, l, v


# ---------- 清单固化 ----------

def test_spec_frozen_16_families_27_keys():
    assert len(INDICATOR_SPEC) == 16
    assert len(FLAT_KEYS) == 28 and len(set(FLAT_KEYS)) == 28
    spec = {s["key"]: s for s in INDICATOR_SPEC}
    assert spec["macd"]["params"] == {"fast": 12, "slow": 26, "signal": 9}
    assert spec["kdj"]["params"] == {"k": 9, "sk": 3, "sd": 3}
    assert spec["boll"]["params"] == {"n": 20, "k": 2}
    assert spec["ma"]["outputs"] == ["ma5", "ma10", "ma20", "ma60"]


def test_series_alignment_and_insufficient_length():
    c, h, l, v = [10.0, 20.0, 30.0, 20.0, 30.0], [11, 21, 31, 21, 31], \
        [9, 19, 29, 19, 29], [100, 200, 300, 400, 500]
    out = compute_series(c, h, l, v, engine="pandas")
    assert set(out) == set(FLAT_KEYS)
    assert all(len(v_) == 5 for v_ in out.values())
    assert out["ma5"][-1] == 22.0            # (10+20+30+20+30)/5
    assert out["obv"] == [100, 300, 600, 200, 700]  # 首值=首根量，涨加跌减
    assert out["ma60"][-1] is None and out["rsi14"][-1] is None  # 长度不足 → None
    assert out["roc12"][-1] is None and out["macd_dif"][-1] is None


# ---------- 口径手算（60 根，独立公式逐因子核对） ----------

def test_hand_check_all_factors():
    c, h, l, v = _series()
    n = len(c)
    out = compute_indicators(c, h, l, v, engine="pandas")

    # MA / EMA / BOLL
    assert math.isclose(out["ma20"], _f_sma_last(c, 20), rel_tol=1e-12)
    e12, e26 = _f_ema(c, 12), _f_ema(c, 26)
    assert math.isclose(out["ema12"], e12[-1], rel_tol=1e-12)
    assert math.isclose(out["ema26"], e26[-1], rel_tol=1e-12)
    win = c[-20:]
    mu = sum(win) / 20
    sd = math.sqrt(sum((x - mu) ** 2 for x in win) / 20)  # 总体标准差
    assert math.isclose(out["boll_mid"], mu, rel_tol=1e-12)
    assert math.isclose(out["boll_upper"], mu + 2 * sd, rel_tol=1e-12)
    assert math.isclose(out["boll_lower"], mu - 2 * sd, rel_tol=1e-12)

    # MACD（经典组合口径）
    dif = [None if (a is None or b is None) else a - b for a, b in zip(e12, e26)]
    dea = _f_ema_from(dif, 9)
    assert math.isclose(out["macd_dif"], dif[-1], rel_tol=1e-12)
    assert math.isclose(out["macd_dea"], dea[-1], rel_tol=1e-12)
    assert math.isclose(out["macd_hist"], dif[-1] - dea[-1], rel_tol=1e-12)

    # KDJ（fastk→SMA3→SMA3→J）
    def fastk(i):
        hh, ll = max(h[i - 8:i + 1]), min(l[i - 8:i + 1])
        return 0.0 if hh == ll else (c[i] - ll) / (hh - ll) * 100

    fk = [fastk(i) for i in range(n - 5, n)]   # fastk 55..59
    k3 = [sum(fk[i - 2:i + 1]) / 3 for i in range(2, 5)]  # k 57..59
    d3 = sum(k3[-3:]) / 3                     # d 59
    assert math.isclose(out["kdj_k"], k3[-1], rel_tol=1e-12)
    assert math.isclose(out["kdj_d"], d3, rel_tol=1e-12)
    assert math.isclose(out["kdj_j"], 3 * k3[-1] - 2 * d3, rel_tol=1e-12)

    # RSI（Wilder）
    for p in (6, 14):
        g = [max(c[i] - c[i - 1], 0.0) for i in range(1, n)]
        z = [max(c[i - 1] - c[i], 0.0) for i in range(1, n)]
        ag = sum(g[:p]) / p
        al = sum(z[:p]) / p
        for i in range(p, len(g)):
            ag = (ag * (p - 1) + g[i]) / p
            al = (al * (p - 1) + z[i]) / p
        assert math.isclose(out[f"rsi{p}"], 100 * ag / (ag + al), rel_tol=1e-12)

    # WR / CCI / BIAS
    hh, ll = max(h[-14:]), min(l[-14:])
    assert math.isclose(out["wr14"], (hh - c[-1]) / (hh - ll) * -100, rel_tol=1e-12)
    tp = [(h[i] + l[i] + c[i]) / 3 for i in range(n - 14, n)]
    tpm = sum(tp) / 14
    md = sum(abs(x - tpm) for x in tp) / 14
    assert math.isclose(out["cci14"], (tp[-1] - tpm) / (0.015 * md), rel_tol=1e-12)
    ma6 = _f_sma_last(c, 6)
    assert math.isclose(out["bias6"], (c[-1] - ma6) / ma6 * 100, rel_tol=1e-12)

    # ATR（tr[1..14] 播种 + Wilder）
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]),
                             abs(l[i] - c[i - 1])) for i in range(1, n)]
    atr = sum(tr[1:15]) / 14
    for i in range(15, n):
        atr = (atr * 13 + tr[i]) / 14
    assert math.isclose(out["atr14"], atr, rel_tol=1e-12)

    # OBV / ROC / MTM
    obv = v[0]
    for i in range(1, n):
        obv += v[i] if c[i] > c[i - 1] else -v[i] if c[i] < c[i - 1] else 0
    assert math.isclose(out["obv"], obv, rel_tol=1e-12)
    assert math.isclose(out["roc12"], (c[-1] - c[-13]) / c[-13] * 100, rel_tol=1e-12)
    assert math.isclose(out["mtm12"], c[-1] - c[-13], rel_tol=1e-12)

    # DMA（dif=SMA10-SMA50，ama=SMA10(dif)）
    difs = [sum(c[i - 9:i + 1]) / 10 - sum(c[i - 49:i + 1]) / 50
            for i in range(n - 10, n)]
    assert math.isclose(out["dma_dif"], difs[-1], rel_tol=1e-12)
    assert math.isclose(out["dma_ama"], sum(difs[-10:]) / 10, rel_tol=1e-12)

    # VR / PSY
    up = down = flat = 0.0
    for i in range(n - 26, n):
        if i == 0 or c[i] == c[i - 1]:
            flat += v[i]
        elif c[i] > c[i - 1]:
            up += v[i]
        else:
            down += v[i]
    assert math.isclose(out["vr26"], (up + 0.5 * flat) / (down + 0.5 * flat) * 100,
                        rel_tol=1e-12)
    ups = sum(1 for i in range(n - 12, n) if c[i] > c[i - 1])
    assert math.isclose(out["psy12"], ups / 12 * 100, rel_tol=1e-12)


# ---------- 两引擎对账（TA-Lib vs pandas，容差 <1e-6） ----------

def test_cross_engine_parity_under_1e_6():
    pytest.importorskip("talib")
    c, h, l, v = _series(n=250, seed=2026)
    pa = PandasTechEngine().compute_series(c, h, l, v)
    ta = get_engine("talib")
    assert ta.name == "talib"
    tb = ta.compute_series(c, h, l, v)
    for k in FLAT_KEYS:
        for i, (x, y) in enumerate(zip(pa[k], tb[k])):
            assert (x is None) == (y is None), f"{k}[{i}] None 不对齐"
            if x is None:
                continue
            assert abs(x - y) <= 1e-6 * max(1.0, abs(y)), \
                f"{k}[{i}] 差异超容差: {x} vs {y}"


# ---------- 引擎选择与降级 ----------

def test_engine_selection_and_fallback(monkeypatch):
    assert get_engine("pandas").name == "pandas"

    # 配置切换生效（§5.4 设置项）
    monkeypatch.setenv("INDICATOR_ENGINE", "pandas")
    get_settings.cache_clear()
    try:
        assert get_engine().name == "pandas"
    finally:
        get_settings.cache_clear()

    # talib 不可用 → 自动降级 pandas 并告警（不抛异常）
    monkeypatch.setitem(sys.modules, "talib", None)
    eng = get_engine("talib")
    assert eng.name == "pandas"


def test_pandas_engine_is_reference_impl():
    assert PandasTechEngine.name == "pandas"
