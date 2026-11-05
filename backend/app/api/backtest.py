"""回测接口（T3.3，开发文档 §7 /api/backtest/*）。

- POST /api/backtest/run：提交异步任务（立即返回 id，状态 queued）
- GET /api/backtest/{id}：状态机查询（queued/running/success/failed）
"""
from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.base import get_db
from app.services.backtest import run_backtest, submit_backtest

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class BacktestRunRequest(BaseModel):
    start_date: date
    end_date: date
    initial_capital: float = Field(100000, gt=0)
    strategy_id: int | None = None
    conditions: dict | None = Field(None, description="选股条件 DSL（ScanSpec JSON）；与 strategy_id 二选一")
    fee_rate: float = Field(0.0005, ge=0)
    slippage: float = Field(0.001, ge=0)
    limit_pct: float = Field(0.10, gt=0, lt=1, description="涨跌停幅度（简化口径，默认 ±10%）")
    max_positions: int = Field(5, ge=1, le=20)


class BacktestStatus(BaseModel):
    id: int
    status: str  # queued / running / success / failed
    strategy_id: int | None = None
    start_date: str | None = None
    end_date: str | None = None
    initial_capital: float
    result: dict = {}
    created_at: str | None = None


@router.post("/run", response_model=BacktestStatus)
def backtest_run(payload: BacktestRunRequest, background: BackgroundTasks,
                 db: Session = Depends(get_db)):
    row = submit_backtest(
        db, start_date=payload.start_date, end_date=payload.end_date,
        initial_capital=payload.initial_capital,
        strategy_id=payload.strategy_id,
        params={"fee_rate": payload.fee_rate, "slippage": payload.slippage,
                "limit_pct": payload.limit_pct,
                "max_positions": payload.max_positions,
                "conditions": payload.conditions},
    )
    background.add_task(run_backtest, row.id)  # 异步执行，立即返回
    return {
        "id": row.id, "status": row.status, "strategy_id": row.strategy_id,
        "start_date": row.start_date.isoformat() if row.start_date else None,
        "end_date": row.end_date.isoformat() if row.end_date else None,
        "initial_capital": row.initial_capital, "result": row.result or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/{backtest_id}", response_model=BacktestStatus)
def backtest_status(backtest_id: int, db: Session = Depends(get_db)):
    from app.models.system import BacktestRecord
    row = db.get(BacktestRecord, backtest_id)
    if row is None:
        raise AppError(404, "回测记录不存在")
    return {
        "id": row.id, "status": row.status, "strategy_id": row.strategy_id,
        "start_date": row.start_date.isoformat() if row.start_date else None,
        "end_date": row.end_date.isoformat() if row.end_date else None,
        "initial_capital": row.initial_capital, "result": row.result or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
