"""回测信号源 — 策略历史回放（T5.6，撮合引擎的 candidate_fn 供体）。

把 T5.1 选股 DSL（ScanSpec）在历史上逐日回放：candidate_fn(D) = D 日收盘按条件
选出的候选（AND 组合，代码升序，确定性）。撮合与组合在 backtest_engine。

口径：
- 技术因子按**不复权价**计算（与 T2.2 指标缓存 / factor_runtime 口径一致，
  选股因子内部自洽）；某日取值 = 该股截至当日最后成交日的指标值（停牌顺延）
- 财务因子 = stock_fin_indicator 最新报告期（≤ 当日）carry-forward（与 T5.2 一致）
- 清单外自定义参数因子（needs_runtime）回测暂不支持（历史逐日实时计算代价过高），
  抛 400 明确提示改用清单内因子（二期可用预计算因子序列扩展）
"""
from __future__ import annotations

import logging
from bisect import bisect_right
from datetime import date

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.market import StockFinIndicator
from app.schemas.selector import FIN_FACTORS, ScanSpec
from app.services.selector import _match_value
from app.services.tech_indicators import compute_series

logger = logging.getLogger("app.backtest_signals")

TECH_WINDOW_NOTE = "full-history"  # 技术因子全历史计算（口径文档注记）


def _fin_series(db: Session, codes: list[str],
                factors: set[str]) -> dict[str, list[tuple[date, dict]]]:
    """每股财务因子时间线（报告期升序），供 as-of 查询。"""
    named = {"roe", "gross_margin", "net_margin", "debt_to_asset", "ocf_to_profit"}
    out: dict[str, list[tuple[date, dict]]] = {c: [] for c in codes}
    for i in range(0, len(codes), 500):
        chunk = codes[i:i + 500]
        rows = (db.query(StockFinIndicator)
                .filter(StockFinIndicator.code.in_(chunk))
                .order_by(StockFinIndicator.code.asc(),
                          StockFinIndicator.report_date.asc())
                .all())
        for r in rows:
            if r.code not in out:
                continue
            vals: dict[str, float] = {}
            for f in factors:
                v = getattr(r, f) if f in named else None
                if v is None:
                    v = (r.metrics or {}).get(f)
                if v is not None:
                    vals[f] = float(v)
            out[r.code].append((r.report_date, vals))
    return out


def build_candidate_fn(db: Session, spec_raw: str | dict,
                       data: dict[str, list[dict]]):
    """构建逐日候选函数：(date) -> list[str]（命中 DSL 的代码，升序）。"""
    try:
        spec = ScanSpec.parse(spec_raw)
    except Exception as e:  # noqa: BLE001 pydantic 错误转 400
        raise AppError(400, f"选股条件非法：{e}")
    if spec.runtime_factors():
        raise AppError(400,
                       f"回测暂不支持清单外自定义参数因子 {spec.runtime_factors()}，"
                       "请改用清单内因子（INDICATOR_SPEC / METRIC_KEYS）")

    tech_needed = {c.factor for c in spec.conditions if c.factor not in FIN_FACTORS}
    fin_needed = {c.factor for c in spec.conditions if c.factor in FIN_FACTORS}

    # 预计算：每股技术因子序列（对齐该股成交日）+ as-of 二分索引
    tech_points: dict[str, dict[str, tuple[list[date], list]]] = {}
    for code, series in (data or {}).items():
        if not series:
            continue
        computed = compute_series(
            [r["close"] for r in series], [r["high"] for r in series],
            [r["low"] for r in series], [r["volume"] for r in series],
        )
        dates = [r["date"] for r in series]
        tech_points[code] = {
            f: (dates, [computed[f][i] for i in range(len(dates))])
            for f in tech_needed
        }

    fin_points: dict[str, tuple[list[date], list[dict]]] = {}
    if fin_needed:
        for code, timeline in _fin_series(db, list(data or {}), fin_needed).items():
            fin_points[code] = ([t[0] for t in timeline], [t[1] for t in timeline])

    def _as_of(points, d: date, keys: tuple[str, ...] = ("value",)):
        """(dates, values) 二分取 ≤d 的最后值；values 为标量列表或 dict 列表。"""
        dates, values = points
        i = bisect_right(dates, d) - 1
        return None if i < 0 else values[i]

    def _tech_value(code: str, f: str, d: date):
        pts = tech_points.get(code, {}).get(f)
        return None if pts is None else _as_of(pts, d)

    def _fin_value(code: str, f: str, d: date):
        pts = fin_points.get(code)
        if pts is None:
            return None
        vals = _as_of(pts, d)
        return None if vals is None else vals.get(f)

    def candidate_fn(day: date) -> list[str]:
        hits: list[str] = []
        for code in sorted(data or {}):
            ok = True
            for c in spec.conditions:
                if c.factor in FIN_FACTORS:
                    v = _fin_value(code, c.factor, day)
                else:
                    v = _tech_value(code, c.factor, day)
                if not _match_value(v, c):
                    ok = False
                    break
            if ok:
                hits.append(code)
        return hits

    return candidate_fn
