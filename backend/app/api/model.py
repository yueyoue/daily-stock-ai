"""估值模型接口（边风炜估值选股 · /api/model）。

三个端点：
- GET  /api/model/rules        规则元数据（权重、阈值、风格清单）
- POST /api/model/scan         全市场扫描（可过滤风格/档位，默认返回前 200）
- GET  /api/model/evaluate/{code}  单票评估（含逐条命中与区间位置）

口径：与 services.bw_model 一致；不修改任何既有接口行为。
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.base import get_db
from app.services import bw_model

router = APIRouter(prefix="/api/model", tags=["model"])


class ScanRequest(BaseModel):
    trade_date: str | None = Field(default=None, description="交易日 YYYY-MM-DD，缺省=库内最新")
    style: str | None = Field(default=None, description="风格过滤：bank/utility/consumer/cyclical/growth/insurance/property")
    verdict: str | None = Field(default=None, description="档位过滤：pass/watch/reject")
    limit: int = Field(default=200, ge=1, le=2000)


@router.get("/rules")
def model_rules():
    """规则元数据（前端规则配置页渲染用；不含任何账号/密钥信息）。"""
    return bw_model.rules_meta()


@router.post("/scan")
def model_scan(payload: ScanRequest, db: Session = Depends(get_db)):
    """全市场扫描。同步返回；数据缺失的门槛在 checks 中标注 skipped。"""
    trade_date: date | None = None
    if payload.trade_date:
        try:
            trade_date = date.fromisoformat(payload.trade_date[:10])
        except ValueError:
            raise AppError(400, "trade_date 需为 YYYY-MM-DD")
    if payload.style and payload.style not in bw_model.RULES:
        raise AppError(400, f"未知风格: {payload.style}")
    if payload.verdict and payload.verdict not in ("pass", "watch", "reject"):
        raise AppError(400, "verdict 仅支持 pass/watch/reject")
    return bw_model.scan(db, trade_date=trade_date, style=payload.style,
                         verdict=payload.verdict, limit=payload.limit)


@router.get("/evaluate/{code}")
def model_evaluate(code: str, trade_date: str | None = None, db: Session = Depends(get_db)):
    """单票评估：档位、分数、逐条规则命中、区间位置、财报红旗。"""
    d: date | None = None
    if trade_date:
        try:
            d = date.fromisoformat(trade_date[:10])
        except ValueError:
            raise AppError(400, "trade_date 需为 YYYY-MM-DD")
    out = bw_model.evaluate(db, code, trade_date=d)
    if out.get("error"):
        raise AppError(404, out["error"])
    return out