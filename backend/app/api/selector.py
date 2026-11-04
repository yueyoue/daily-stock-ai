"""选股扫描接口（T3.3，开发文档 §7 /api/selector/scan，同步 ≤20s 预算）。

T5.3 追加策略管理：/api/selector/strategies CRUD（stock_strategy 命名保存/复用）。
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.base import get_db
from app.models.system import StockStrategy
from app.schemas.selector import ScanSpec
from app.services.selector import run_scan

router = APIRouter(prefix="/api/selector", tags=["selector"])


class Condition(BaseModel):
    """T5.1 DSL 条目：比较符 × 阈值；between 用 value~value2 区间。"""
    factor: str = Field(description="因子 key（28 技术 + 30 财务 + 族内自定义参数）")
    op: str = Field(description=">、>=、<、<=、==、!=、between")
    value: float
    value2: float | None = None


class ScanRequest(BaseModel):
    conditions: list[Condition] = Field(min_length=1)
    trade_date: str | None = None


class Candidate(BaseModel):
    code: str
    date: str | None = None
    matched: list[str]
    metrics: dict
    runtime_computed: list[str] = []  # 清单外因子实时兜底标注（§5.3）


class ScanResponse(BaseModel):
    scan_ms: int
    budget_s: float
    conditions: list[dict]
    trade_date: str | None = None
    snapshot_source: str = "realtime"
    warnings: list[str] = []
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


# ---------- 策略保存与管理（T5.3） ----------

class StrategyIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    conditions: list[Condition] = Field(min_length=1)


class StrategyView(BaseModel):
    id: int
    name: str
    conditions: list[dict]
    created_at: str | None = None


def _spec_or_400(conditions: list[Condition]) -> dict:
    """条件合法性用 T5.1 DSL 校验（非法条件 400 明确错误）；存储格式=DSL JSON。"""
    try:
        spec = ScanSpec.parse({"logic": "and", "conditions": [c.model_dump() for c in conditions]})
    except Exception as e:  # noqa: BLE001 校验错误统一 400
        raise AppError(400, str(e))
    return spec.model_dump()


def _strategy_view(r: StockStrategy) -> dict:
    stored = r.conditions or {}
    conds = [
        {k: v for k, v in c.items() if not (k == "value2" and v is None)}
        for c in stored.get("conditions", [])
    ]
    return {
        "id": r.id,
        "name": r.name,
        "conditions": conds,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.get("/strategies", response_model=dict)
def strategies_list(db: Session = Depends(get_db)):
    rows = db.query(StockStrategy).order_by(StockStrategy.id.asc()).all()
    return {"items": [_strategy_view(r) for r in rows], "total": len(rows)}


@router.post("/strategies", response_model=StrategyView)
def strategies_create(payload: StrategyIn, db: Session = Depends(get_db)):
    dup = db.query(StockStrategy).filter(StockStrategy.name == payload.name).first()
    if dup is not None:
        raise AppError(409, "策略名已存在")
    row = StockStrategy(name=payload.name, conditions=_spec_or_400(payload.conditions))
    db.add(row)
    db.commit()
    return _strategy_view(row)


@router.put("/strategies/{strategy_id}", response_model=StrategyView)
def strategies_update(strategy_id: int, payload: StrategyIn, db: Session = Depends(get_db)):
    row = db.get(StockStrategy, strategy_id)
    if row is None:
        raise AppError(404, "策略不存在")
    if payload.name != row.name:
        dup = db.query(StockStrategy).filter(StockStrategy.name == payload.name).first()
        if dup is not None:
            raise AppError(409, "策略名已存在")
    row.name = payload.name
    row.conditions = _spec_or_400(payload.conditions)
    db.commit()
    return _strategy_view(row)


@router.delete("/strategies/{strategy_id}")
def strategies_delete(strategy_id: int, db: Session = Depends(get_db)):
    row = db.get(StockStrategy, strategy_id)
    if row is None:
        raise AppError(404, "策略不存在")
    db.delete(row)
    db.commit()
    return {"ok": True}
