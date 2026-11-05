"""回测任务服务（T3.3 异步状态机；引擎 T5.5~T5.7；T5.8 任务队列与进度）。

状态机：queued（排队/等并发槽位）→ running（运行）→ success（成功）/ failed（失败）。
- submit_backtest：建任务行并排队（状态 queued）
- submit_backtest_task：投递到**统一后台任务队列**（T3.6 task_runner 独立线程 +
  task_log 留痕 + 失败告警）；HTTP 接口立即返回，长周期（20 年）回测不受请求超时影响
- run_backtest：单次执行（并发闸门 → running → 引擎 → success/failed）
- 进度：引擎逐交易日回传 (done, total)，节流写回 BacktestRecord.result.progress，
  GET /api/backtest/{id} 可实时查询（前端进度条）

口径决策（记录于进度文档 T5.8 备注）：
- **并发闸门**：`backtest_max_concurrent`（默认 2）个回测同时执行，超出的任务保持
  queued 等待（真正的"入队列"，而非无界并发拖垮进程）；等待超过
  `backtest_queue_timeout_s` 置 failed 并留因；
- **队列层不重试**：回测失败多为数据/参数问题（复权因子缺失、策略不存在、区间无数据等），
  重跑代价高（长周期分钟级），故 attempts=1，仅做 task_log 留痕 + 失败告警（T7.6 接 webhook）。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.models.base import SessionLocal
from app.models.system import BacktestRecord
from app.services.task_runner import run_task, submit_background

logger = logging.getLogger("app.backtest")

STATUSES = ("queued", "running", "success", "failed")

PROGRESS_MIN_INTERVAL_S = 1.0  # 进度写库节流间隔（秒）

_QUEUE_LOCK = threading.Lock()
_QUEUE: threading.Semaphore | None = None


def _queue() -> threading.Semaphore:
    """回测并发闸门（懒加载单例；配置变更/测试可 reset_queue() 后重建）。"""
    global _QUEUE
    with _QUEUE_LOCK:
        if _QUEUE is None:
            slots = max(1, int(getattr(get_settings(), "backtest_max_concurrent", 2) or 2))
            _QUEUE = threading.Semaphore(slots)
        return _QUEUE


def reset_queue() -> None:
    """重建并发闸门（配置变更/测试用）。"""
    global _QUEUE
    with _QUEUE_LOCK:
        _QUEUE = None


def submit_backtest(db: Session, *, start_date: date, end_date: date,
                    initial_capital: float = 100000.0,
                    strategy_id: int | None = None,
                    params: dict | None = None) -> BacktestRecord:
    """提交回测任务（参数校验后入队，状态 queued）。"""
    if end_date < start_date:
        raise AppError(400, "end_date 不能早于 start_date")
    if initial_capital <= 0:
        raise AppError(400, "initial_capital 必须为正数")
    params = dict(params or {})
    for key in ("fee_rate", "slippage"):
        if float(params.get(key, 0) or 0) < 0:
            raise AppError(400, f"{key} 不能为负")
    row = BacktestRecord(
        strategy_id=strategy_id, start_date=start_date, end_date=end_date,
        initial_capital=initial_capital, params=params, status="queued", result={},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---------- 进度写回（T5.8，异步可查） ----------

def _write_progress(record_id: int, done: int, total: int) -> None:
    """把进度写回任务行（独立会话）：GET /api/backtest/{id} 运行中即可读到。"""
    db = SessionLocal()
    try:
        row = db.get(BacktestRecord, record_id)
        if row is None or row.status not in ("queued", "running"):
            return
        result = dict(row.result or {})
        result["progress"] = {
            "done": int(done), "total": int(total),
            "percent": round(done / total * 100, 1) if total else 0.0,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        row.result = result
        db.commit()
    except Exception:  # noqa: BLE001 进度写失败不影响回测本身
        logger.debug("回测 %s 进度写回失败", record_id, exc_info=True)
    finally:
        db.close()


def _progress_writer(record_id: int):
    """节流后的进度回调：(done, total) → 写库（末次必写）。"""
    last = [0.0]

    def cb(done: int, total: int) -> None:
        now = time.monotonic()
        if done < total and now - last[0] < PROGRESS_MIN_INTERVAL_S:
            return
        last[0] = now
        _write_progress(record_id, done, total)

    return cb


# ---------- 引擎 ----------

def _engine(row: BacktestRecord, progress_cb=None) -> dict:
    """回测引擎（T5.5 数据口径 + T5.6 撮合与组合 + T5.7 统计）。"""
    from app.models.system import StockStrategy
    from app.services.backtest_data import load_universe_series
    from app.services.backtest_engine import TradeRules, simulate
    from app.services.backtest_signals import build_candidate_fn

    params = row.params or {}
    db = SessionLocal()
    try:
        spec_raw = params.get("conditions")
        if not spec_raw and row.strategy_id is not None:
            strat = db.get(StockStrategy, row.strategy_id)
            if strat is None:
                raise ValueError(f"选股策略 {row.strategy_id} 不存在")
            spec_raw = strat.conditions or {}
        if not spec_raw:
            raise ValueError("缺少选股策略条件（strategy_id 或 params.conditions）")

        data, skipped = load_universe_series(db, row.start_date, row.end_date)
        candidate_fn = build_candidate_fn(db, spec_raw, data)
        rules = TradeRules(
            slippage=float(params.get("slippage", 0.001)),
            fee_rate=float(params.get("fee_rate", 0.0005)),
            limit_pct=float(params.get("limit_pct", 0.10)),
            max_positions=int(params.get("max_positions", 5)),
        )
        out = simulate(data, candidate_fn, row.start_date, row.end_date,
                       row.initial_capital, rules, progress_cb=progress_cb)
        from app.services.backtest_stats import compute_stats
        stats = compute_stats(out["trades"], out["equity_curve"], row.initial_capital)
        return {
            "engine": "t5.6",
            "equity_curve": out["equity_curve"],
            "trades": out["trades"],
            "trade_details": stats["trade_details"],
            "events": out["events"],
            "final_equity": out["final_equity"],
            "open_orders": out["open_orders"],
            "skipped_codes": skipped,
            "rules": {"slippage": rules.slippage, "fee_rate": rules.fee_rate,
                      "limit_pct": rules.limit_pct,
                      "max_positions": rules.max_positions},
            "win_rate": stats["win_rate"],
            "profit_loss_ratio": stats["profit_loss_ratio"],
            "max_drawdown": stats["max_drawdown"],
            "cumulative_return": stats["cumulative_return"],
            "closed_trades": stats["closed_trades"],
            "risk_notice": "回测基于历史数据模拟，不代表未来收益",
        }
    finally:
        db.close()


# ---------- 执行 ----------

def _mark_failed(record_id: int, error: str) -> None:
    db = SessionLocal()
    try:
        row = db.get(BacktestRecord, record_id)
        if row is None:
            return
        row.status = "failed"
        row.result = {"error": error[:500]}
        db.commit()
    finally:
        db.close()


def run_backtest(record_id: int) -> bool:
    """单次执行回测（后台线程/脚本调用）：并发闸门 → running → 引擎 → success/failed。

    返回是否成功（不抛异常，失败原因写 result.error）；队列等待超时直接置 failed。
    """
    wait_s = float(getattr(get_settings(), "backtest_queue_timeout_s", 1800.0) or 1800.0)
    gate = _queue()
    if not gate.acquire(timeout=wait_s):
        _mark_failed(record_id, f"排队等待超过 {wait_s:.0f}s 未获得执行槽位（并发上限 "
                                f"{getattr(get_settings(), 'backtest_max_concurrent', 2)}）")
        logger.warning("回测任务 %s 排队超时", record_id)
        return False
    try:
        db = SessionLocal()
        try:
            row = db.get(BacktestRecord, record_id)
            if row is None:
                logger.error("回测任务 %s 不存在", record_id)
                return False
            if row.status not in ("queued", "running"):
                logger.info("回测任务 %s 状态 %s，跳过执行", record_id, row.status)
                return row.status == "success"
            row.status = "running"
            row.result = dict(row.result or {})
            db.commit()

            t0 = time.monotonic()
            try:
                result = _engine(row, progress_cb=_progress_writer(record_id))
                elapsed = int((time.monotonic() - t0) * 1000)
                days = len(result.get("equity_curve") or [])
                result["duration_ms"] = elapsed
                result["progress"] = {"done": days, "total": days, "percent": 100.0,
                                      "updated_at": datetime.now().isoformat(timespec="seconds")}
                row = db.get(BacktestRecord, record_id)  # 进度写回过 → 重取避免覆盖
                row.result = result
                row.status = "success"
                db.commit()
                return True
            except Exception as e:  # noqa: BLE001 引擎异常 → failed 留因
                logger.exception("回测任务 %s 失败", record_id)
                row = db.get(BacktestRecord, record_id)
                row.result = {"error": str(e)[:500],
                              "duration_ms": int((time.monotonic() - t0) * 1000)}
                row.status = "failed"
                db.commit()
                return False
        finally:
            db.close()
    finally:
        gate.release()


def _execute(db, record_id: int) -> None:
    """task_runner 任务体：回测失败 → 抛错，由 run_task 记 failed 并触发告警。"""
    if not run_backtest(record_id):
        raise RuntimeError(f"回测任务 {record_id} 执行失败（详见记录 result.error）")


def run_backtest_task(record_id: int) -> None:
    """任务队列入口（T3.6 执行器）：task_log 留痕 + 失败告警（不重试，见模块口径）。"""
    run_task(f"回测任务#{record_id}", _execute, record_id=record_id, attempts=1)


def submit_backtest_task(record_id: int) -> threading.Thread:
    """投递回测到统一后台任务队列（HTTP 立即返回；长周期不阻塞请求线程）。"""
    return submit_background(f"回测任务#{record_id}", _execute,
                             record_id=record_id, attempts=1)