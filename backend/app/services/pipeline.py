"""采集流水线（任务 T1.10）：任务编排 + 清洗入库 + task_log 全程记录。

三种模式：
- full        全量：stock_info + kline(20年回填) + finance + market_daily
- incremental 日更：stock_info + kline 尾部增量(跳过退市标的) + market_daily
- single      单数据类型：stock_info / kline / finance / market_daily(=三条市场线合成)

清洗规则（模块1"清洗：去重、异常值过滤、停牌标记、数据对齐"）：
- 去重/数据对齐：各 upsert 先到先得 + 标准化日期归一（T1.6~T1.9 已内建）
- 异常值过滤：K 线价格非正、高低价倒挂的记录丢弃并计数
- 停牌标记：停牌日天然无 K 线行（不造假日线）；退市标记由 stock_info 全量刷新维护

task_log 记录：顶层任务（含计时基线）+ 采集/清洗/入库分阶段 + 单标的失败明细。

⚠️ 缺陷 D-01 修复：
- log_stage 遇 pending-rollback 会话先回滚再重建重试（否则日志写入被
  PendingRollbackError 打断，真实错误被掩盖）；
- 各 _run_* 的 running 阶段行在成功或失败时都被终结为终态（不再永久停留 running）。
"""
import logging
import time
from datetime import date

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.market import StockInfo
from app.models.system import TaskLog
from app.services.collect_chain import collect_with_fallback
from app.services.finance import sync_finance_for_code, upsert_finance
from app.services.kline import sync_kline_for_code
from app.services.market import sync_market_daily
from app.services.stock_info import upsert_stock_info

logger = logging.getLogger("app.pipeline")

MODES = ("full", "incremental", "single")
SINGLE_TYPES = ("stock_info", "kline", "finance", "market_daily",
                "moneyflow", "limit_updown", "market_summary")


def log_stage(db: Session, task_name: str, status: str, duration_ms: int = 0,
              message: str = "") -> TaskLog:
    """写一条 task_log 阶段行（遇 pending-rollback 会话先回滚再重建重试，D-01）。"""
    def _make() -> TaskLog:
        return TaskLog(task_name=task_name[:64], status=status,
                       duration_ms=duration_ms, message=message[:2000])

    row = _make()
    db.add(row)
    try:
        db.commit()
    except Exception:
        db.rollback()
        row = _make()
        db.add(row)
        db.commit()
    return row


def _finalize_log(db: Session, row_id: int | None, status: str, duration_ms: int,
                  message: str, task_name: str) -> None:
    """按 id 把已建的 task_log 行终结为终态（先回滚再重新附着），避免停留 running（D-01）。

    行已不存在时退化为新建一条，保证终态一定有记录。
    """
    try:
        db.rollback()
        row = db.get(TaskLog, row_id) if row_id is not None else None
        if row is not None:
            row.status = status
            row.duration_ms = duration_ms
            row.message = message[:2000]
            db.commit()
            return
    except Exception:
        db.rollback()
    log_stage(db, task_name, status, duration_ms, message)


def run_pipeline(db: Session, mode: str, data_type: str | None = None,
                 run_date: date | None = None, task_id: int | None = None) -> dict:
    """执行采集流水线，返回汇总统计（同时写 task_log）。

    task_id：复用已建任务行（/api/collect/trigger 的 queued 行），否则新建。
    """
    if mode not in MODES:
        raise AppError(400, f"mode 需为 {MODES} 之一")
    if mode == "single" and data_type not in SINGLE_TYPES:
        raise AppError(400, f"single 模式 data_type 需为 {SINGLE_TYPES} 之一")
    run_date = run_date or date.today()

    top_name = f"采集任务:{mode}" + (f":{data_type}" if data_type else "")
    if task_id is not None:
        top = db.get(TaskLog, task_id)
        if top is None:
            top = log_stage(db, f"采集任务:{mode}", "running")
        else:
            top.status = "running"
            db.commit()
    else:
        top = log_stage(db, top_name, "running", message=f"date={run_date}")
    top_id = top.id
    t0 = time.monotonic()
    stats: dict = {"mode": mode, "data_type": data_type, "date": run_date.isoformat()}
    try:
        if mode == "single":
            _run_single(db, data_type, run_date, stats)
        else:
            _run_stock_info(db, stats)
            _run_kline(db, force_full=(mode == "full"), stats=stats, run_date=run_date)
            if mode == "full":
                _run_finance(db, stats)
            _run_market_daily(db, run_date, stats)
        duration_ms = int((time.monotonic() - t0) * 1000)
        _finalize_log(db, top_id, "success", duration_ms, f"完成 {stats}", top_name)
        stats["task_id"] = top_id
        stats["duration_ms"] = duration_ms
        logger.info("采集流水线 %s 完成：%s", mode, stats)
        return stats
    except Exception as e:
        duration_ms = int((time.monotonic() - t0) * 1000)
        _finalize_log(db, top_id, "failed", duration_ms, f"失败: {e}", top_name)
        logger.exception("采集流水线 %s 失败", mode)
        raise


def _run_single(db: Session, data_type: str, run_date: date, stats: dict) -> None:
    if data_type == "stock_info":
        _run_stock_info(db, stats)
    elif data_type == "kline":
        _run_kline(db, force_full=True, stats=stats, run_date=run_date)
    elif data_type == "finance":
        _run_finance(db, stats)
    else:  # market_daily / moneyflow / limit_updown / market_summary
        types = ("moneyflow", "limit_updown", "market_summary") if data_type == "market_daily" \
            else (data_type,)
        _run_market_daily(db, run_date, stats, data_types=types)


def _run_stock_info(db: Session, stats: dict) -> None:
    t0 = time.monotonic()
    stage_id = log_stage(db, "采集:stock_info", "running").id
    try:
        records = collect_with_fallback(db, "stock_info")
    except Exception as e:
        _finalize_log(db, stage_id, "failed", int((time.monotonic() - t0) * 1000),
                      str(e), "采集:stock_info")
        raise
    _finalize_log(db, stage_id, "success", int((time.monotonic() - t0) * 1000),
                  f"{len(records)} 条", "采集:stock_info")
    # 标准化去重/退市标记在入库服务内建（full_refresh 带量级护栏）
    t1 = time.monotonic()
    log_stage(db, "清洗:stock_info", "success", 0, "去重与退市标记随入库执行")
    s = upsert_stock_info(db, records)
    log_stage(db, "入库:stock_info", "success", int((time.monotonic() - t1) * 1000), f"{s}")
    stats["stock_info"] = s


def _run_kline(db: Session, force_full: bool, stats: dict,
               run_date: date | None = None) -> None:
    codes = [c for (c,) in db.query(StockInfo.code).all()]
    delisted = {c for (c,) in db.query(StockInfo.code).filter(StockInfo.is_delisted.is_(True)).all()}
    t0 = time.monotonic()
    stage_id = log_stage(db, "采集:kline", "running", message=f"{len(codes)} 只标的").id
    agg = {"codes": 0, "skipped": 0, "received": 0, "inserted": 0, "updated": 0,
           "dropped": 0, "failed": 0}
    try:
        for code in codes:
            if not force_full and code in delisted:
                agg["skipped"] += 1  # 退市标的不再有新 K 线（全量模式仍回填历史）
                continue
            try:
                s = sync_kline_for_code(db, code, end_date=run_date, force_full=force_full)
            except AppError as e:
                agg["failed"] += 1
                log_stage(db, f"入库:kline:{code}", "failed", 0, e.message)
                continue
            agg["codes"] += 1
            for k in ("received", "inserted", "updated", "dropped"):
                agg[k] += s[k]
    except Exception as e:
        _finalize_log(db, stage_id, "failed", int((time.monotonic() - t0) * 1000),
                      str(e), "采集:kline")
        raise
    _finalize_log(db, stage_id, "success", int((time.monotonic() - t0) * 1000),
                  f"{agg}", "采集:kline")
    log_stage(db, "清洗:kline", "success", 0,
              f"异常值过滤丢弃 {agg['dropped']} 条（去重/日期对齐随标准化，停牌日无K线不造假日线）")
    log_stage(db, "入库:kline", "success", 0, f"{agg}")
    stats["kline"] = agg


def _run_finance(db: Session, stats: dict) -> None:
    codes = [c for (c,) in db.query(StockInfo.code).all()]
    t0 = time.monotonic()
    stage_id = log_stage(db, "采集:finance", "running", message=f"{len(codes)} 只标的").id
    agg = {"codes": 0, "received": 0, "inserted": 0, "updated": 0, "failed": 0}
    try:
        for code in codes:
            try:
                s = sync_finance_for_code(db, code)
            except AppError as e:
                agg["failed"] += 1
                log_stage(db, f"入库:finance:{code}", "failed", 0, e.message)
                continue
            agg["codes"] += 1
            for k in ("received", "inserted", "updated"):
                agg[k] += s[k]
    except Exception as e:
        _finalize_log(db, stage_id, "failed", int((time.monotonic() - t0) * 1000),
                      str(e), "采集:finance")
        raise
    _finalize_log(db, stage_id, "success", int((time.monotonic() - t0) * 1000),
                  f"{agg}", "采集:finance")
    log_stage(db, "入库:finance", "success", 0, f"{agg}")
    stats["finance"] = agg


def _run_market_daily(db: Session, run_date: date, stats: dict,
                      data_types: tuple = ("moneyflow", "limit_updown", "market_summary")) -> None:
    t0 = time.monotonic()
    stage_id = log_stage(db, "采集:market_daily", "running").id
    try:
        s = sync_market_daily(db, day=run_date, data_types=data_types)
    except Exception as e:
        _finalize_log(db, stage_id, "failed", int((time.monotonic() - t0) * 1000),
                      str(e), "采集:market_daily")
        raise
    _finalize_log(db, stage_id, "success", int((time.monotonic() - t0) * 1000),
                  f"{s}", "采集:market_daily")
    log_stage(db, "入库:market_daily", "success", 0, f"{s}")
    stats["market_daily"] = s