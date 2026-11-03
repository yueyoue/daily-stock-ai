"""仪表盘接口（T3.2，开发文档 §7 /api/dashboard/market）。"""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models.base import get_db
from app.services.dashboard import get_market_dashboard

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


class SectorFund(BaseModel):
    name: str
    moneyflow: float | None = None
    heat: str | None = None


class TrendPoint(BaseModel):
    date: str
    sentiment_score: float | None = None


class DashboardMarket(BaseModel):
    """大盘首页：四卡片 + 情绪趋势 + 主线板块（§3.2；主力资金=东财口径）。"""

    trade_date: str | None = None
    sentiment_score: float | None = None
    limit_up_count: int | None = None
    limit_down_count: int | None = None
    max_consecutive: int | None = None
    consecutive_ladder: dict = {}
    main_moneyflow: float | None = None
    up_count: int | None = None
    down_count: int | None = None
    sentiment_trend: list[TrendPoint] = []
    main_sectors: list[SectorFund] = []


@router.get("/market", response_model=DashboardMarket)
def dashboard_market(
    days: int = Query(20, ge=5, le=120, description="情绪趋势窗口（交易日）"),
    db: Session = Depends(get_db),
):
    return get_market_dashboard(db, days=days)
