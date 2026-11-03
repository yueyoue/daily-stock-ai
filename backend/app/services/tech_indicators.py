"""技术指标 16 项 · 双引擎（T2.2，开发文档 §4 模块2 / §5.4）。

- INDICATOR_SPEC：16 项指标（族）参数清单**固化**——该清单 = 模式 B 下选股策略
  可用因子清单（FLAT_KEYS 展平 28 个标量因子，T5.1 对齐、T5.2 缓存覆盖范围）
- 双引擎：
  * TalibTechEngine（主）：调用 TA-Lib C 实现
  * PandasTechEngine（降级）：pandas/纯 Python 复刻 TA-Lib 口径（含 EMA 的
    SMA 播种、RSI/ATR 的 Wilder 平滑、KDJ 的 STOCH(9,3,3) 口径）
- 配置切换：Settings.indicator_engine（"talib" | "pandas"，默认 talib）；
  请求 talib 但环境无 TA-Lib 时**自动降级 pandas 并告警**（不阻塞批处理）
- 两引擎结果一致性（容差 <1e-6 相对误差）由 tests/test_tech_indicators.py
  对账用例把关（有 TA-Lib 的环境跑双引擎对账，CI 装 TA-Lib 轮子直接验证）

口径注意：
- KDJ 按 TA-Lib STOCH(9,3,3) 口径（快速 K + SMA 平滑，输出自第 12 根起），与通达信
  KDJ（递归平滑）存在平滑差异——选股因子与回测内部统一用本清单口径，保证自洽；
- MACD 采用经典口径（DIF=EMA12−EMA26、DEA=EMA9(DIF)），两引擎一致；TA-Lib 的
  MACD 函数播种口径特殊（早期值不稳定），故主引擎用 talib.EMA 组合而非 talib.MACD
  （文档化差异，保证两引擎 <1e-6 一致）。
"""
from __future__ import annotations

import logging
import math

from app.core.config import get_settings

logger = logging.getLogger("app.tech_indicators")

# ---------- 16 项指标参数清单（固化 = 因子清单基准） ----------
INDICATOR_SPEC: list[dict] = [
    {"key": "ma", "name": "移动平均线", "params": {"periods": [5, 10, 20, 60]},
     "outputs": ["ma5", "ma10", "ma20", "ma60"],
     "formula": "SMA(close, n)：n 日简单移动平均"},
    {"key": "ema", "name": "指数移动平均", "params": {"periods": [12, 26]},
     "outputs": ["ema12", "ema26"],
     "formula": "EMA(close, n)：alpha=2/(n+1)，前 n 根 SMA 播种（TA-Lib 口径）"},
    {"key": "macd", "name": "MACD", "params": {"fast": 12, "slow": 26, "signal": 9},
     "outputs": ["macd_dif", "macd_dea", "macd_hist"],
     "formula": "dif=EMA12-EMA26；dea=EMA9(dif)；hist=dif-dea"},
    {"key": "kdj", "name": "KDJ", "params": {"k": 9, "sk": 3, "sd": 3},
     "outputs": ["kdj_k", "kdj_d", "kdj_j"],
     "formula": "fastk=RSV(9)=(C-LLV9)/(HHV9-LLV9)*100；K=SMA3(fastk)；D=SMA3(K)；J=3K-2D（TA-Lib STOCH 口径）"},
    {"key": "rsi", "name": "相对强弱", "params": {"periods": [6, 14]},
     "outputs": ["rsi6", "rsi14"],
     "formula": "RSI(n)：Wilder 平滑涨跌幅均值，100*avgGain/(avgGain+avgLoss)"},
    {"key": "boll", "name": "布林带", "params": {"n": 20, "k": 2},
     "outputs": ["boll_upper", "boll_mid", "boll_lower"],
     "formula": "mid=SMA20；upper/lower=mid±2*总体标准差(20)"},
    {"key": "wr", "name": "威廉指标", "params": {"n": 14},
     "outputs": ["wr14"],
     "formula": "WR(14)=(HHV14-C)/(HHV14-LLV14)*-100"},
    {"key": "cci", "name": "顺势指标", "params": {"n": 14},
     "outputs": ["cci14"],
     "formula": "CCI(14)=(TP-SMA(TP,14))/(0.015*平均绝对偏差)"},
    {"key": "bias", "name": "乖离率", "params": {"n": 6},
     "outputs": ["bias6"],
     "formula": "BIAS(6)=(C-SMA6)/SMA6*100"},
    {"key": "atr", "name": "真实波幅", "params": {"n": 14},
     "outputs": ["atr14"],
     "formula": "ATR(14)：Wilder 平滑真实波幅 TR=max(H-L,|H-PC|,|L-PC|)"},
    {"key": "obv", "name": "能量潮", "params": {},
     "outputs": ["obv"],
     "formula": "OBV：收盘上涨累加成交量、下跌累减、持平不变"},
    {"key": "roc", "name": "变动率", "params": {"n": 12},
     "outputs": ["roc12"],
     "formula": "ROC(12)=(C-C12)/C12*100"},
    {"key": "mtm", "name": "动量线", "params": {"n": 12},
     "outputs": ["mtm12"],
     "formula": "MTM(12)=C-C12"},
    {"key": "dma", "name": "平均线差", "params": {"short": 10, "long": 50, "m": 10},
     "outputs": ["dma_dif", "dma_ama"],
     "formula": "dif=SMA10-SMA50；ama=SMA10(dif)"},
    {"key": "vr", "name": "成交量比率", "params": {"n": 26},
     "outputs": ["vr26"],
     "formula": "VR(26)=(上涨量+0.5*平盘量)/(下跌量+0.5*平盘量)*100"},
    {"key": "psy", "name": "心理线", "params": {"n": 12},
     "outputs": ["psy12"],
     "formula": "PSY(12)=12 日内上涨天数/12*100"},
]

FLAT_KEYS: tuple[str, ...] = tuple(k for spec in INDICATOR_SPEC for k in spec["outputs"])
assert len(INDICATOR_SPEC) == 16 and len(set(FLAT_KEYS)) == len(FLAT_KEYS)


# ---------- 公共小工具（两引擎共用的组合口径） ----------

def _none_tail(values) -> list:
    """float('nan') / nan → None（JSON 友好），其余保留。"""
    out = []
    for v in values:
        if v is None:
            out.append(None)
        else:
            fv = float(v)
            out.append(None if math.isnan(fv) else fv)
    return out


def _compose_bias(closes: list, ma: list, n: int) -> list:
    return [None if (m is None or not m) else (c - m) / m * 100
            for c, m in zip(closes, ma)]


def _compose_j(k: list, d: list) -> list:
    return [None if (a is None or b is None) else 3 * a - 2 * b for a, b in zip(k, d)]


# ---------- pandas 降级引擎（复刻 TA-Lib 口径） ----------

class PandasTechEngine:
    """pandas/纯 Python 实现（TA-Lib 口径复刻，作为降级引擎）。"""

    name = "pandas"

    @staticmethod
    def _sma(x: list, n: int) -> list:
        out = [None] * len(x)
        if n <= 0 or len(x) < n:
            return out
        run = 0.0
        for i, v in enumerate(x):
            run += v
            if i >= n:
                run -= x[i - n]
            if i >= n - 1:
                out[i] = run / n
        return out

    @staticmethod
    def _ema(x: list, n: int) -> list:
        """TA-Lib EMA 口径：前 n 根 SMA 播种，alpha=2/(n+1) 递推。"""
        out = [None] * len(x)
        if len(x) < n or n <= 0:
            return out
        alpha = 2.0 / (n + 1)
        out[n - 1] = sum(x[:n]) / n
        for i in range(n, len(x)):
            out[i] = alpha * x[i] + (1 - alpha) * out[i - 1]
        return out

    @staticmethod
    def _ema_of_series(y: list, n: int) -> list:
        """对已带 None 头部的序列做 EMA（跳过 None 段，首个有效值起 SMA 播种）。"""
        start = next((i for i, v in enumerate(y) if v is not None), None)
        if start is None:
            return [None] * len(y)
        seg = [v for v in y[start:] if v is not None]
        tail = PandasTechEngine._ema(seg, n)
        out = [None] * len(y)
        for i, v in enumerate(tail, start=start):
            out[i] = v
        return out

    @staticmethod
    def _sma_of_series(y: list, n: int) -> list:
        """对带 None 头部的序列做 SMA（跳过 None 段，仅在有效段上平滑）。"""
        start = next((i for i, v in enumerate(y) if v is not None), None)
        if start is None:
            return [None] * len(y)
        seg = [v for v in y[start:] if v is not None]
        tail = PandasTechEngine._sma(seg, n)
        out = [None] * len(y)
        for i, v in enumerate(tail, start=start):
            out[i] = v
        return out

    @staticmethod
    def _wilder(x: list, n: int) -> list:
        """Wilder 平滑：首值=前 n 个均值，之后 prev*(n-1)+cur 除 n。"""
        out = [None] * len(x)
        if len(x) < n or n <= 0:
            return out
        out[n - 1] = sum(x[:n]) / n
        for i in range(n, len(x)):
            out[i] = (out[i - 1] * (n - 1) + x[i]) / n
        return out

    def compute_series(self, closes, highs, lows, volumes) -> dict[str, list]:
        c = [float(v) for v in closes]
        h = [float(v) for v in highs]
        lo = [float(v) for v in lows]
        vol = [float(v) for v in volumes]
        n = len(c)
        out: dict[str, list] = {}

        for p in (5, 10, 20, 60):
            out[f"ma{p}"] = self._sma(c, p)
        for p in (12, 26):
            out[f"ema{p}"] = self._ema(c, p)

        # MACD
        dif = [None if (a is None or b is None) else a - b
               for a, b in zip(out["ema12"], out["ema26"])]
        dea = self._ema_of_series(dif, 9)
        out["macd_dif"], out["macd_dea"] = dif, dea
        out["macd_hist"] = [None if (a is None or b is None) else a - b
                            for a, b in zip(dif, dea)]

        # KDJ（STOCH(9,3,3) 口径，输出自快线起点+双平滑跨度起，对齐 TA-Lib STOCH）
        fastk = [None] * n
        for i in range(n):
            if i < 8:
                continue
            hh = max(h[i - 8:i + 1])
            ll = min(lo[i - 8:i + 1])
            fastk[i] = 0.0 if hh == ll else (c[i] - ll) / (hh - ll) * 100
        k = self._sma_of_series(fastk, 3)
        d = self._sma_of_series(k, 3)
        valid_from = 8 + 2 + 2  # 与 talib.STOCH 输出起点一致（看回溯值）
        k = [v if i >= valid_from else None for i, v in enumerate(k)]
        d = [v if i >= valid_from else None for i, v in enumerate(d)]
        out["kdj_k"], out["kdj_d"] = k, d
        out["kdj_j"] = _compose_j(k, d)

        # RSI（Wilder）
        for p in (6, 14):
            gains = [0.0] * n
            losses = [0.0] * n
            for i in range(1, n):
                ch = c[i] - c[i - 1]
                gains[i] = max(ch, 0.0)
                losses[i] = max(-ch, 0.0)
            ag = self._wilder(gains[1:], p)
            al = self._wilder(losses[1:], p)
            rsi = [None] * n
            for i in range(1, n):
                g, l = ag[i - 1], al[i - 1]
                if g is None or l is None:
                    continue
                rsi[i] = 100.0 if g + l == 0 else 100.0 * g / (g + l)
            out[f"rsi{p}"] = rsi

        # BOLL（总体标准差 ddof=0）
        mid = self._sma(c, 20)
        upper = [None] * n
        lower = [None] * n
        for i in range(19, n):
            win = c[i - 19:i + 1]
            mu = sum(win) / 20
            sd = math.sqrt(sum((v - mu) ** 2 for v in win) / 20)
            upper[i] = mid[i] + 2 * sd
            lower[i] = mid[i] - 2 * sd
        out["boll_upper"], out["boll_mid"], out["boll_lower"] = upper, mid, lower

        # WR / CCI / ATR
        wr = [None] * n
        cci = [None] * n
        for i in range(n):
            if i < 13:
                continue
            hh = max(h[i - 13:i + 1])
            ll = min(lo[i - 13:i + 1])
            wr[i] = 0.0 if hh == ll else (hh - c[i]) / (hh - ll) * -100
            tp = [(h[j] + lo[j] + c[j]) / 3 for j in range(i - 13, i + 1)]
            mu = sum(tp) / 14
            md = sum(abs(v - mu) for v in tp) / 14
            cci[i] = 0.0 if md == 0 else (tp[-1] - mu) / (0.015 * md)
        out["wr14"], out["cci14"] = wr, cci

        tr = [h[0] - lo[0]] + [
            max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1]))
            for i in range(1, n)
        ]
        # TA-Lib ATR 口径：首值=tr[1..n] 均值（自第 n 根起），之后 Wilder 递推
        out["atr14"] = [None] + self._wilder(tr[1:], 14)

        # BIAS / OBV / ROC / MTM / DMA / VR / PSY（组合口径，两引擎共用算法）
        out["bias6"] = _compose_bias(c, self._sma(c, 6), 6)
        obv = [vol[0]] + [0.0] * (n - 1)  # TA-Lib OBV 口径：首值=首根成交量
        for i in range(1, n):
            obv[i] = obv[i - 1] + (vol[i] if c[i] > c[i - 1]
                                   else -vol[i] if c[i] < c[i - 1] else 0.0)
        out["obv"] = obv
        out["roc12"] = [None if i < 12 or not c[i - 12]
                        else (c[i] - c[i - 12]) / c[i - 12] * 100 for i in range(n)]
        out["mtm12"] = [None if i < 12 else c[i] - c[i - 12] for i in range(n)]

        ma10, ma50 = self._sma(c, 10), self._sma(c, 50)
        dma_dif = [None if (a is None or b is None) else a - b
                   for a, b in zip(ma10, ma50)]
        dma_ama = self._sma_of_series(dma_dif, 10)
        out["dma_dif"], out["dma_ama"] = dma_dif, dma_ama

        vr = [None] * n
        psy = [None] * n
        for i in range(n):
            if i >= 25:
                up = down = flat = 0.0
                for j in range(i - 25, i + 1):
                    if j == 0:
                        flat += vol[j]
                    elif c[j] > c[j - 1]:
                        up += vol[j]
                    elif c[j] < c[j - 1]:
                        down += vol[j]
                    else:
                        flat += vol[j]
                den = down + 0.5 * flat
                vr[i] = None if den == 0 else (up + 0.5 * flat) / den * 100
            if i >= 11:
                ups = sum(1 for j in range(i - 11, i + 1)
                          if j > 0 and c[j] > c[j - 1])
                psy[i] = ups / 12 * 100
        out["vr26"], out["psy12"] = vr, psy

        return {k: _none_tail(v) for k, v in out.items()}


# ---------- TA-Lib 主引擎 ----------

class TalibTechEngine:
    """TA-Lib C 实现（主引擎）。组合口径（BIAS/DMA/VR/PSY）共用 pandas 侧算法。"""

    name = "talib"

    def __init__(self):
        import numpy as np
        import talib
        self._np = np
        self._talib = talib

    def compute_series(self, closes, highs, lows, volumes) -> dict[str, list]:
        np, talib = self._np, self._talib
        c = np.asarray(closes, dtype=float)
        h = np.asarray(highs, dtype=float)
        lo = np.asarray(lows, dtype=float)
        vol = np.asarray(volumes, dtype=float)
        out: dict[str, list] = {}

        for p in (5, 10, 20, 60):
            out[f"ma{p}"] = talib.SMA(c, timeperiod=p)
        for p in (12, 26):
            out[f"ema{p}"] = talib.EMA(c, timeperiod=p)

        dif, dea, hist = self._macd(c)
        out["macd_dif"], out["macd_dea"], out["macd_hist"] = dif, dea, hist

        k, d = talib.STOCH(h, lo, c, fastk_period=9, slowk_period=3,
                           slowk_matype=0, slowd_period=3, slowd_matype=0)
        out["kdj_k"], out["kdj_d"] = k, d
        out["kdj_j"] = np.asarray(_compose_j(_none_tail(k), _none_tail(d)), dtype=float)

        for p in (6, 14):
            out[f"rsi{p}"] = talib.RSI(c, timeperiod=p)

        upper, mid, lower = talib.BBANDS(c, timeperiod=20, nbdevup=2, nbdevdn=2, matype=0)
        out["boll_upper"], out["boll_mid"], out["boll_lower"] = upper, mid, lower

        out["wr14"] = talib.WILLR(h, lo, c, timeperiod=14)
        out["cci14"] = talib.CCI(h, lo, c, timeperiod=14)
        out["atr14"] = talib.ATR(h, lo, c, timeperiod=14)

        ma6 = talib.SMA(c, timeperiod=6)
        out["bias6"] = np.asarray(
            _compose_bias(_none_tail(c), _none_tail(ma6), 6), dtype=float)
        out["obv"] = talib.OBV(c, vol)
        out["roc12"] = talib.ROC(c, timeperiod=12)
        out["mtm12"] = talib.MOM(c, timeperiod=12)

        dma_dif = talib.SMA(c, timeperiod=10) - talib.SMA(c, timeperiod=50)
        out["dma_dif"] = dma_dif
        out["dma_ama"] = np.asarray(PandasTechEngine._sma_of_series(
            _none_tail(dma_dif), 10), dtype=float)

        out["vr26"], out["psy12"] = self._vr_psy(c, vol)
        return {k: _none_tail(v) for k, v in out.items()}

    def _macd(self, c):
        """经典 MACD 组合（talib.EMA 播种，与 PandasTechEngine 同口径）。"""
        dif = self._talib.EMA(c, timeperiod=12) - self._talib.EMA(c, timeperiod=26)
        dea = PandasTechEngine._ema_of_series(_none_tail(dif), 9)
        import numpy as np
        dea = np.asarray(dea, dtype=float)
        hist = dif - dea
        return dif, dea, hist

    @staticmethod
    def _vr_psy(c, vol):
        """VR/PSY 无 TA-Lib 对应 → 两引擎共用 Python 实现（口径一致）。"""
        c_list, v_list = _none_tail(c), _none_tail(vol)
        n = len(c_list)
        vr = [None] * n
        psy = [None] * n
        for i in range(n):
            if i >= 25:
                up = down = flat = 0.0
                for j in range(i - 25, i + 1):
                    if j == 0:
                        flat += v_list[j]
                    elif c_list[j] > c_list[j - 1]:
                        up += v_list[j]
                    elif c_list[j] < c_list[j - 1]:
                        down += v_list[j]
                    else:
                        flat += v_list[j]
                den = down + 0.5 * flat
                vr[i] = None if den == 0 else (up + 0.5 * flat) / den * 100
            if i >= 11:
                ups = sum(1 for j in range(i - 11, i + 1)
                          if j > 0 and c_list[j] > c_list[j - 1])
                psy[i] = ups / 12 * 100
        return vr, psy


# ---------- 引擎选择与对外 API ----------

def get_engine(name: str | None = None) -> TalibTechEngine | PandasTechEngine:
    """引擎选择：配置项切换生效；talib 不可用时自动降级 pandas 并告警。"""
    want = (name or get_settings().indicator_engine or "talib").strip().lower()
    if want == "talib":
        try:
            return TalibTechEngine()
        except ImportError:
            logger.warning("TA-Lib 不可用，技术指标引擎降级为 pandas 实现（§5.4 降级用）")
    return PandasTechEngine()


def compute_series(closes, highs, lows, volumes,
                   engine: str | None = None) -> dict[str, list]:
    """全序列 28 因子计算（T2.3 缓存管线用）；key 对齐 FLAT_KEYS，头部未定义为 None。"""
    eng = get_engine(engine)
    result = eng.compute_series(closes, highs, lows, volumes)
    missing = set(FLAT_KEYS) - set(result)
    assert not missing, f"引擎 {eng.name} 缺因子: {missing}"
    return result


def compute_indicators(closes, highs, lows, volumes,
                       engine: str | None = None) -> dict[str, float | None]:
    """最新一根 K 线的 28 因子取值（随查随出）。"""
    series = compute_series(closes, highs, lows, volumes, engine=engine)
    return {k: (v[-1] if v else None) for k, v in series.items()}
