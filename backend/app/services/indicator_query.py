"""指标查询服务（T2.5，开发文档 §4 模块2 对外接口）。

- query_code：单标的指标查询（走 T2.4 统一取数：库表/缓存/实时兜底）
- factor_snapshot：全市场因子快照（模式 B→缓存 / 模式 A→库表）
- scan_factor：全市场单因子筛选底层（供 T5.2 选股器调用；走缓存毫秒级，
  缺缓存→逐标的实时计算慢路径兜底 + task_log 告警）

验收口径：全市场（≈5300 只）单因子筛选走缓存 ≤1s（实测约 3ms 量级）。
"""
from __future__ import annotations

import logging
import time
from datetime import date

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.market import StockKline, StockTechIndicator
from app.services.indicator_store import (
    _realtime_metrics,
    get_code_indicators,
    store_mode_enabled,
)
from app.services.pipeline import log_stage
from app.services.tech_indicators import FLAT_KEYS

logger = logging.getLogger("app.indicator_query")

_OPS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def query_code(db: Session, code: str, trade_date: date | None = None,
               factors: list[str] | None = None) -> dict | None:
    """单标的指标查询（含统一取数兜底）；factors 为空返回全部 28 因子。"""
    got = get_code_indicators(db, code, trade_date=trade_date)
    if got is None:
        return None
    if factors:
        unknown = [f for f in factors if f not in FLAT_KEYS]
        if unknown:
            raise AppError(400, f"未知因子: {unknown}；可用因子见 INDICATOR_SPEC")
        got = {**got, "metrics": {f: got["metrics"][f] for f in factors}}
    return got


def factor_snapshot(db: Session, trade_date: date | None = None) -> dict | None:
    """全市场因子快照（模式 B→指标缓存 / 模式 A→库表）；无数据返回 None。"""
    if store_mode_enabled():
        q = db.query(StockTechIndicator)
        rows = q.filter_by(date=trade_date).all() if trade_date else q.all()
        if not rows:
            return None
        d = trade_date or max(r.date for r in rows)
        rows = [r for r in rows if r.date == d]
        return {
            "meta": {"date": d.isoformat(), "rows": len(rows), "source": "db",
                     "indicators": list(FLAT_KEYS)},
            "rows": [{"code": r.code, "date": r.date.isoformat(), **(r.metrics or {})}
                     for r in rows],
        }
    from app.services.indicator_cache import load_cache
    return load_cache(trade_date)


def _slow_snapshot(db: Session, trade_date: date | None,
                   engine: str | None = None) -> list[dict]:
    """缺缓存慢路径：逐标的实时计算（慢但正确），告警一次。"""
    q = db.query(StockKline.code).distinct()
    if trade_date is not None:
        q = q.filter(StockKline.date <= trade_date)
    codes = [r[0] for r in q.all()]
    log_stage(db, "指标兜底:scan", "warning",
              message=f"date={trade_date or 'latest'} 缺缓存，全市场实时计算慢路径（{len(codes)} 只）")
    logger.warning("指标缓存缺失，全市场扫描走实时计算慢路径（%d 只）", len(codes))
    rows: list[dict] = []
    for code in codes:
        metrics, d = _realtime_metrics(db, code, trade_date, engine=engine)
        if metrics is not None:
            rows.append({"code": code, "date": d.isoformat() if d else None, **metrics})
    return rows


def scan_factor(db: Session, factor: str, op: str, value: float,
                trade_date: date | None = None,
                engine: str | None = None) -> list[dict]:
    """全市场单因子筛选（底层）：返回满足 条件 的行（按 code 排序）。"""
    if factor not in FLAT_KEYS:
        raise AppError(400, f"未知因子: {factor}；可用因子见 INDICATOR_SPEC")
    if op not in _OPS:
        raise AppError(400, f"未知比较符: {op}；可用 {sorted(_OPS)}")
    t0 = time.monotonic()
    snap = factor_snapshot(db, trade_date)
    rows = snap["rows"] if snap else _slow_snapshot(db, trade_date, engine=engine)
    hits = [r for r in rows
            if r.get(factor) is not None and _OPS[op](r[factor], value)]
    hits.sort(key=lambda r: r["code"])
    logger.info("单因子筛选 %s %s %s：%d/%d 命中，耗时 %dms",
                factor, op, value, len(hits), len(rows),
                int((time.monotonic() - t0) * 1000))
    return hits
