"""全市场扫描执行器（T5.2，开发文档 §4 模块4）。

- 条件走 T5.1 DSL（财务因子/技术因子 × 比较符 × 阈值/区间，AND）
- 扫描走**指标缓存快照**（不联网，模式 B 缓存 / 模式 A 库表）；缺缓存慢路径实时计算
- 财务因子取 stock_fin_indicator 最新报告期（≤ trade_date）
- 清单外因子（族内自定义参数）→ 幸存候选上实时计算兜底并**标注**（§5.3）
- **20 秒预算监控**：阶段间检查，超预算抛 504 明确错误
- 输出：候选池 + 匹配条件明细（describe）+ 因子取值 + 兜底标注
"""
from __future__ import annotations

import logging
import time
from datetime import date

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.models.market import StockFinIndicator, StockInfo, StockKline
from app.schemas.selector import FIN_FACTORS, FactorCondition, ScanSpec
from app.services.factor_runtime import compute_factor
from app.services.indicator_query import (
    _OPS as COMPARE_OPS,
    _slow_snapshot,
    factor_snapshot,
)

logger = logging.getLogger("app.selector")

RUNTIME_BARS = 260  # 实时兜底计算窗口（约一年日线）


def _ensure_budget(deadline: float, budget_s: float) -> None:
    if time.monotonic() > deadline:
        raise AppError(504, f"扫描超时（超过 {budget_s:g}s 预算），请收紧条件后重试")


def _match_value(value: float | None, c: FactorCondition) -> bool:
    """单因子命中判定（含 between 区间）。None 值视为不命中。"""
    if value is None:
        return False
    if c.op == "between":
        return c.value <= value <= c.value2
    return COMPARE_OPS[c.op](value, c.value)


def _fin_factor_map(db: Session, codes: list[str], factors: set[str],
                    trade_date: date | None) -> dict[str, dict[str, float]]:
    """最新报告期（≤ trade_date）财务因子取值；分块查询（SQLite 参数上限）。"""
    named = {"roe", "gross_margin", "net_margin", "debt_to_asset", "ocf_to_profit"}
    out: dict[str, dict[str, float]] = {}
    for i in range(0, len(codes), 500):
        chunk = codes[i:i + 500]
        q = db.query(StockFinIndicator).filter(StockFinIndicator.code.in_(chunk))
        if trade_date is not None:
            q = q.filter(StockFinIndicator.report_date <= trade_date)
        rows = q.order_by(StockFinIndicator.code.asc(), desc(StockFinIndicator.report_date)).all()
        for r in rows:
            if r.code in out:
                continue  # 已取到该标的最新报告期
            vals: dict[str, float] = {}
            for f in factors:
                v = getattr(r, f) if f in named else None
                if v is None:
                    v = (r.metrics or {}).get(f)  # T2.1 metrics 含全量 30 项，双路取值
                if v is not None:
                    vals[f] = float(v)
            out[r.code] = vals
    return out


def _runtime_series(db: Session, code: str, trade_date: date | None) -> dict[str, list[float]]:
    """取最近 RUNTIME_BARS 根 K 线（≤ trade_date）作实时兜底计算输入。"""
    q = db.query(StockKline).filter(StockKline.code == code)
    if trade_date is not None:
        q = q.filter(StockKline.date <= trade_date)
    rows = q.order_by(desc(StockKline.date)).limit(RUNTIME_BARS).all()
    rows.reverse()
    return {
        "closes": [r.close for r in rows],
        "highs": [r.high for r in rows],
        "lows": [r.low for r in rows],
        "volumes": [r.volume for r in rows],
    }


def run_scan(db: Session, conditions: list[dict], trade_date=None) -> dict:
    """执行扫描，返回候选池 + 匹配条件明细（同步，超预算 504）。"""
    try:
        spec = ScanSpec.parse({"logic": "and", "conditions": conditions or []})
    except Exception as e:  # noqa: BLE001 DSL 校验错误 → 400 明确提示
        raise AppError(400, str(e))

    budget_s = float(get_settings().selector_scan_budget_s)
    started, deadline = time.monotonic(), time.monotonic() + budget_s

    tech_conds = [c for c in spec.conditions if not c.needs_runtime and c.factor not in FIN_FACTORS]
    fin_conds = [c for c in spec.conditions if c.factor in FIN_FACTORS]
    runtime_conds = [c for c in spec.conditions if c.needs_runtime]
    warnings: list[str] = []

    # ① 技术因子：缓存/库表快照（不联网），缺缓存走实时计算慢路径
    snap = factor_snapshot(db, trade_date)
    source = (snap or {}).get("meta", {}).get("source", "realtime") if snap else "realtime"
    rows = snap["rows"] if snap else _slow_snapshot(db, trade_date)
    for c in tech_conds:
        rows = [r for r in rows if _match_value(r.get(c.factor), c)]
        _ensure_budget(deadline, budget_s)

    # ② 财务因子：最新报告期（≤ trade_date）
    fin_map: dict[str, dict[str, float]] = {}
    if fin_conds and rows:
        fin_map = _fin_factor_map(db, [r["code"] for r in rows],
                                  {c.factor for c in fin_conds}, trade_date)
        for c in fin_conds:
            rows = [r for r in rows
                    if _match_value(fin_map.get(r["code"], {}).get(c.factor), c)]
            _ensure_budget(deadline, budget_s)

    # ③ 清单外因子：幸存候选逐标的实时计算兜底（标注）
    runtime_values: dict[str, dict[str, float]] = {}
    if runtime_conds and rows:
        warnings.append(f"{len(rows)} 只候选 × {len(runtime_conds)} 个清单外因子走实时计算兜底")
        survived = []
        for r in rows:
            series = _runtime_series(db, r["code"], trade_date)
            vals: dict[str, float] = {}
            ok = True
            for c in runtime_conds:
                v = compute_factor(c.factor, series["closes"], series["highs"],
                                   series["lows"], series["volumes"])
                if not _match_value(v, c):
                    ok = False
                    break
                vals[c.factor] = v
            if ok:
                runtime_values[r["code"]] = vals
                survived.append(r)
            _ensure_budget(deadline, budget_s)
        rows = survived

    candidates = []
    for r in rows:
        metrics = {c.factor: r.get(c.factor) for c in tech_conds}
        metrics.update({c.factor: fin_map.get(r["code"], {}).get(c.factor) for c in fin_conds})
        metrics.update(runtime_values.get(r["code"], {}))
        candidates.append({
            "code": r["code"],
            "name": "",
            "date": r.get("date"),
            "matched": [c.describe() for c in spec.conditions],
            "metrics": metrics,
            "runtime_computed": sorted(runtime_values.get(r["code"], {})),
        })

    # 名称补全（T5.4 结果表）：stock_info 批量查，分块兼容 SQLite 参数上限
    codes = [c["code"] for c in candidates]
    names: dict[str, str] = {}
    for i in range(0, len(codes), 500):
        chunk = codes[i:i + 500]
        names.update(dict(db.query(StockInfo.code, StockInfo.name)
                          .filter(StockInfo.code.in_(chunk)).all()))
    for c in candidates:
        c["name"] = names.get(c["code"], "")

    scan_ms = int((time.monotonic() - started) * 1000)
    logger.info("选股扫描：%d 条件（技术%d/财务%d/实时%d），%d 候选，耗时 %dms（预算 %.0fs）",
                len(spec.conditions), len(tech_conds), len(fin_conds), len(runtime_conds),
                len(candidates), scan_ms, budget_s)
    return {
        "scan_ms": scan_ms,
        "budget_s": budget_s,
        "conditions": [c.model_dump() for c in spec.conditions],
        "trade_date": trade_date.isoformat() if trade_date else None,
        "snapshot_source": source,
        "warnings": warnings,
        "total": len(candidates),
        "candidates": candidates,
    }
