"""选股扫描接口（T3.3，开发文档 §7 /api/selector/scan，同步 ≤20s 预算）。"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models.base import get_db
from app.services.selector import run_scan

router = APIRouter(prefix="/api/selector", tags=["selector"])


class Condition(BaseModel):
    factor: str = Field(description="因子 key（T5.1 前仅技术因子）")
    op: str = Field(description=">、>=、<、<=、==、!=")
    value: float


class ScanRequest(BaseModel):
    conditions: list[Condition] = Field(min_length=1)
    trade_date: str | None = None


class Candidate(BaseModel):
    code: str
    date: str | None = None
    matched: list[str]
    metrics: dict


class ScanResponse(BaseModel):
    scan_ms: int
    budget_s: float
    conditions: list[dict]
    total: int
    candidates: list[Candidate]


@router.post("/scan", response_model=ScanResponse)
def selector_scan(payload: ScanRequest, db: Session = Depends(get_db)):
    trade_date = None
    if payload.trade_date:
        from datetime import date as _date
        trade_date = _date.fromisoformat(payload.trade_date[:10])
    return run_scan(db, [c.model_dump() for c in payload.conditions],
                    trade_date=trade_date)
