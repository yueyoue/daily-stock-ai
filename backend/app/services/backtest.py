"""回测任务服务（T3.3 异步状态机；回测引擎 T5.5~T5.8 接入）。

状态机：queued（排队）→ running（运行）→ success（成功）/ failed（失败）。
- submit_backtest：建任务行并排队；执行由调用方以后台任务触发 run_backtest
- run_backtest：running → 引擎执行 → success/failed（失败原因写 result.error）
- 当前引擎为**骨架**（输出净值曲线/胜率/盈亏比/最大回撤/交易记录空结构），
  T5.5~T5.8 替换 _engine 部分；API/状态机不变
"""
from __future__ import annotations

import logging
import time
from datetime import date

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.base import SessionLocal
from app.models.system import BacktestRecord

logger = logging.getLogger("app.backtest")

STATUSES = ("queued", "running", "success", "failed")


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


def _engine(row: BacktestRecord) -> dict:
    """回测引擎（T5.5 数据口径 + T5.6 撮合与组合；统计指标 T5.7 接入）。"""
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
                       row.initial_capital, rules)
        return {
            "engine": "t5.6",
            "equity_curve": out["equity_curve"],
            "trades": out["trades"],
            "events": out["events"],
            "final_equity": out["final_equity"],
            "open_orders": out["open_orders"],
            "skipped_codes": skipped,
            "rules": {"slippage": rules.slippage, "fee_rate": rules.fee_rate,
                      "limit_pct": rules.limit_pct,
                      "max_positions": rules.max_positions},
            "win_rate": None, "profit_loss_ratio": None, "max_drawdown": None,
            "risk_notice": "回测基于历史数据模拟，不代表未来收益",
        }
    finally:
        db.close()


def run_backtest(record_id: int) -> None:
    """执行回测任务（后台调用）：状态机推进 queued/running → success/failed。"""
    db = SessionLocal()
    try:
        row = db.get(BacktestRecord, record_id)
        if row is None:
            logger.error("回测任务 %s 不存在", record_id)
            return
        row.status = "running"
        db.commit()
        t0 = time.monotonic()
        try:
            result = _engine(row)
            result["duration_ms"] = int((time.monotonic() - t0) * 1000)
            row.result = result
            row.status = "success"
        except Exception as e:  # noqa: BLE001 引擎异常 → failed 留因
            logger.exception("回测任务 %s 失败", record_id)
            row.result = {"error": str(e)[:500]}
            row.status = "failed"
        db.commit()
    finally:
        db.close()
