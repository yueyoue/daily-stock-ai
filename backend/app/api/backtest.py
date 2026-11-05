"""回测接口（T3.3；T5.8 长周期异步化 —— 任务队列 + 进度可查）。

- POST /api/backtest/run：提交任务并**投递到统一后台任务队列**后立即返回
  （状态 queued；20 年长周期回测不受 HTTP 请求超时限制）
- GET /api/backtest/{id}：状态机查询（queued/running/success/failed）+ 运行进度
  （result.progress = {done, total, percent, updated_at}，前端进度条数据源）
"""
from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.base import get_db
from app.services.backtest import submit_backtest, submit_backtest_task

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
    progress: dict = Field(default_factory=dict,
                           description="运行进度 {done,total,percent,updated_at}（T5.8）")
    result: dict = {}
    created_at: str | None = None


def _view(row) -> dict:
    result = row.result or {}
    return {
        "id": row.id, "status": row.status, "strategy_id": row.strategy_id,
        "start_date": row.start_date.isoformat() if row.start_date else None,
        "end_date": row.end_date.isoformat() if row.end_date else None,
        "initial_capital": row.initial_capital,
        "progress": result.get("progress") or {},
        "result": result,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.post("/run", response_model=BacktestStatus)
def backtest_run(payload: BacktestRunRequest, db: Session = Depends(get_db)):
    row = submit_backtest(
        db, start_date=payload.start_date, end_date=payload.end_date,
        initial_capital=payload.initial_capital,
        strategy_id=payload.strategy_id,
        params={"fee_rate": payload.fee_rate, "slippage": payload.slippage,
                "limit_pct": payload.limit_pct,
                "max_positions": payload.max_positions,
                "conditions": payload.conditions},
    )
    submit_backtest_task(row.id)  # T5.8：统一任务队列后台执行，HTTP 立即返回
    return _view(row)


@router.get("/{backtest_id}", response_model=BacktestStatus)
def backtest_status(backtest_id: int, db: Session = Depends(get_db)):
    from app.models.system import BacktestRecord
    row = db.get(BacktestRecord, backtest_id)
    if row is None:
        raise AppError(404, "回测记录不存在")
    return _view(row)