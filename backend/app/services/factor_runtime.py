"""清单外因子实时计算兜底（T5.2，开发文档 §5.3）。

族内自定义参数（ma30/rsi24 等 INDICATOR_SPEC 清单外因子）纯 Python 计算，
只对**已通过缓存因子筛选的幸存候选**逐标的调用（量小，保 20s 预算）。

口径与 T2.2 双引擎一致：MA=SMA、EMA=SMA 播种、RSI=Wilder 平滑；
bias/roc/mtm/psy/wr/atr/cci/vr 为标准定义（兜底近似口径随代码文档化）。
输入序列为**不复权价**（与 T2.2 指标口径一致，选股因子内部自洽）。
"""
from __future__ import annotations

import re

FAMILY_RE = re.compile(r"^(ma|ema|rsi|atr|cci|wr|bias|roc|mtm|psy|vr)(\d+)$")


def parse_factor_key(factor: str) -> tuple[str | None, int | None]:
    m = FAMILY_RE.match(factor or "")
    return (m.group(1), int(m.group(2))) if m else (None, None)


def sma(vals: list[float], n: int) -> float | None:
    return sum(vals[-n:]) / n if len(vals) >= n else None


def ema(vals: list[float], n: int) -> float | None:
    """EMA：前 n 根 SMA 播种（与 T2.2 PandasTechEngine 一致）。"""
    if len(vals) < n:
        return None
    e = sum(vals[:n]) / n
    k = 2.0 / (n + 1)
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


def rsi(vals: list[float], n: int) -> float | None:
    """RSI：Wilder 平滑（与 T2.2 一致）。"""
    if len(vals) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(vals)):
        ch = vals[i] - vals[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def atr(highs: list[float], lows: list[float], closes: list[float], n: int) -> float | None:
    """ATR：Wilder 平滑的真实波幅均值。"""
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def cci(highs: list[float], lows: list[float], closes: list[float], n: int) -> float | None:
    tp = [(h + l + c) / 3.0 for h, l, c in zip(highs, lows, closes)]
    if len(tp) < n:
        return None
    window = tp[-n:]
    mean = sum(window) / n
    md = sum(abs(x - mean) for x in window) / n
    return (tp[-1] - mean) / (0.015 * md) if md else 0.0


def wr(highs: list[float], lows: list[float], closes: list[float], n: int) -> float | None:
    if len(closes) < n:
        return None
    hh, ll = max(highs[-n:]), min(lows[-n:])
    if hh == ll:
        return -50.0
    return -100.0 * (hh - closes[-1]) / (hh - ll)


def compute_factor(
    factor: str,
    closes: list[float],
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    volumes: list[float] | None = None,
) -> float | None:
    """计算单个族内因子值；序列不足/未知因子返回 None。"""
    fam, n = parse_factor_key(factor)
    if fam is None or n <= 0:
        return None
    highs = highs or closes
    lows = lows or closes
    volumes = volumes or []
    if fam == "ma":
        return sma(closes, n)
    if fam == "ema":
        return ema(closes, n)
    if fam == "rsi":
        return rsi(closes, n)
    if fam == "atr":
        return atr(highs, lows, closes, n)
    if fam == "cci":
        return cci(highs, lows, closes, n)
    if fam == "wr":
        return wr(highs, lows, closes, n)
    if fam == "bias":
        m = sma(closes, n)
        return (closes[-1] - m) / m * 100.0 if m else None
    if fam == "roc":
        return (closes[-1] / closes[-1 - n] - 1.0) * 100.0 if len(closes) >= n + 1 else None
    if fam == "mtm":
        return closes[-1] - closes[-1 - n] if len(closes) >= n + 1 else None
    if fam == "psy":
        if len(closes) < n + 1:
            return None
        ups = sum(1 for i in range(-n, 0) if closes[i] > closes[i - 1])
        return ups / n * 100.0
    if fam == "vr":
        # VR 兜底近似：上涨日成交量和 / 下跌日成交量和 × 100（无下跌日给 200 上限）
        if len(closes) < n + 1 or not volumes:
            return None
        up = sum(volumes[i] for i in range(-n, 0) if closes[i] > closes[i - 1])
        down = sum(volumes[i] for i in range(-n, 0) if closes[i] < closes[i - 1])
        return up / down * 100.0 if down else (200.0 if up else 100.0)
    return None
