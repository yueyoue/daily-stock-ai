"""指标存储模式与失效兜底（T2.4，开发文档 §5.3 兜底 / §5.4 开关）。

- 模式 A（tech_indicator_store=true）：指标写 `stock_tech_indicator` 并每日增量
- 模式 B（默认）：不入库，读 T2.3 指标缓存（Parquet）
- 统一取数入口 `get_code_indicators`：A→库表 / B→缓存；**缺失/缺因子→实时计算
  兜底（慢但正确）+ task_log 告警**，两模式口径同源（T2.2 双引擎），数据一致

兜底计算取本地 K 线库（不联网）；告警写 task_log（"指标兜底:*"，warning），
供 T4.6 任务监控页展示。模式 A 的每日增量由 T7.5 调度接线（16:00 批处理）。
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.market import StockKline, StockTechIndicator
from app.services.pipeline import log_stage
from app.services.tech_indicators import FLAT_KEYS, get_engine

logger = logging.getLogger("app.indicator_store")


def store_mode_enabled(db: Session | None = None) -> bool:
    """模式 A 开关（§5.4 技术指标入库开关，默认关=模式 B）。

    系统配置页可运行时切换（system_config 覆盖层，T3.5）；不传 db 时读环境变量默认值。
    """
    if db is not None:
        from app.services.config_store import get_override

        override = get_override(db, "tech_indicator_store")
        if override is not None:
            return bool(override)
    return bool(get_settings().tech_indicator_store)


# ---------- 模式 A：入库与增量 ----------

def sync_tech_indicators_for_code(db: Session, code: str,
                                  engine: str | None = None) -> dict:
    """单标的技术指标入库（全量历史按日落 stock_tech_indicator，增量幂等）。"""
    bars = (
        db.query(StockKline)
        .filter(StockKline.code == code)
        .order_by(StockKline.date.asc())
        .all()
    )
    if not bars:
        return {"code": code, "periods": 0, "written": 0, "skipped": 0}

    eng = get_engine(engine)
    series = eng.compute_series(
        [b.close for b in bars], [b.high for b in bars],
        [b.low for b in bars], [b.volume for b in bars],
    )
    existing = {
        r.date: r
        for r in db.query(StockTechIndicator).filter_by(code=code).all()
    }
    written = skipped = 0
    for i, bar in enumerate(bars):
        metrics = {k: (series[k][i] if series[k] else None) for k in FLAT_KEYS}
        row = existing.get(bar.date)
        if row is not None and (row.metrics or {}) == metrics:
            skipped += 1  # 增量：未变不重写
            continue
        if row is None:
            row = StockTechIndicator(code=code, date=bar.date)
            db.add(row)
        row.metrics = metrics
        written += 1
    db.commit()
    logger.info("[%s] 技术指标入库：计算 %d 日，写 %d 跳 %d",
                code, len(bars), written, skipped)
    return {"code": code, "periods": len(bars), "written": written, "skipped": skipped}


def sync_tech_indicators_all(db: Session, engine: str | None = None) -> dict:
    """全市场批量增量入库（模式 A 每日批处理入口）。"""
    codes = [r[0] for r in db.query(StockKline.code).distinct().all()]
    totals = {"codes": len(codes), "periods": 0, "written": 0, "skipped": 0}
    for code in codes:
        stats = sync_tech_indicators_for_code(db, code, engine=engine)
        for k in ("periods", "written", "skipped"):
            totals[k] += stats[k]
    return totals


# ---------- 统一取数（含失效兜底） ----------

def _realtime_metrics(db: Session, code: str, trade_date=None,
                      engine: str | None = None) -> tuple[dict, object] | tuple[None, None]:
    """实时计算兜底：取本地 K 线（≤trade_date）算指标（不联网，慢但正确）。"""
    q = db.query(StockKline).filter(StockKline.code == code)
    if trade_date is not None:
        q = q.filter(StockKline.date <= trade_date)
    bars = q.order_by(StockKline.date.asc()).all()
    if not bars:
        return None, None
    eng = get_engine(engine)
    series = eng.compute_series(
        [b.close for b in bars], [b.high for b in bars],
        [b.low for b in bars], [b.volume for b in bars],
    )
    metrics = {k: (series[k][-1] if series[k] else None) for k in FLAT_KEYS}
    return metrics, bars[-1].date


def _warn_fallback(db: Session, code: str, reason: str) -> None:
    log_stage(db, f"指标兜底:{code}", "warning", message=reason[:500])
    logger.warning("[%s] 指标兜底实时计算：%s", code, reason)


def get_code_indicators(db: Session, code: str, trade_date=None,
                        engine: str | None = None) -> dict | None:
    """统一取数：模式 A→库表 / 模式 B→缓存；缺失/缺因子→实时计算兜底+告警。

    返回 {code, date, source, metrics}；source ∈ {db, cache, realtime}。
    """
    from app.services.indicator_cache import load_cache

    metrics = None
    source = None

    if store_mode_enabled(db):  # 模式 A：库表
        if trade_date is not None:
            row = db.get(StockTechIndicator, (code, trade_date))
        else:  # 未指定日期 → 取最新
            row = (
                db.query(StockTechIndicator)
                .filter_by(code=code)
                .order_by(StockTechIndicator.date.desc())
                .first()
            )
        if row is not None and set(row.metrics or {}) >= set(FLAT_KEYS):
            metrics, source = dict(row.metrics), "db"
            trade_date = trade_date or row.date
    else:  # 模式 B：指标缓存
        cache = load_cache(trade_date)
        row = next((r for r in (cache or {}).get("rows", []) if r["code"] == code), None)
        if row is not None and set(row) >= set(FLAT_KEYS):
            metrics = {k: row[k] for k in FLAT_KEYS}
            source = "cache"

    if metrics is None:  # 缓存缺失/缺因子 → 实时计算兜底
        metrics, calc_date = _realtime_metrics(db, code, trade_date, engine=engine)
        if metrics is None:
            return None
        source = "realtime"
        trade_date = trade_date or calc_date
        _warn_fallback(db, code,
                       f"mode={'A' if store_mode_enabled(db) else 'B'} "
                       f"date={trade_date or 'latest'} 缺缓存/缺因子，已实时计算兜底")

    return {"code": code, "date": trade_date, "source": source, "metrics": metrics}
