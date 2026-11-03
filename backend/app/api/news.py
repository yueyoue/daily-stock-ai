"""资讯接口（T3.4，开发文档 §7 /api/news/list；采集与降噪 T6.x）。

过滤条件：板块（关键词模糊）/ 关键词 / 日期区间 / 影响等级 / 来源。
"""
from datetime import date, datetime, time as dtime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.api.deps import get_pagination
from app.models.base import get_db
from app.models.content import News
from app.schemas.common import Pagination

router = APIRouter(prefix="/api/news", tags=["news"])


class NewsItem(BaseModel):
    id: int
    published_at: str | None = None
    title: str
    content: str
    source: str
    impact_level: str
    url: str


class NewsPage(BaseModel):
    items: list[NewsItem]
    total: int
    page: int
    page_size: int


@router.get("/list", response_model=NewsPage)
def news_list(
    keyword: str | None = Query(None, description="标题/内容模糊匹配（含板块名）"),
    impact_level: str | None = Query(None, description="高/中/低/未分级"),
    source: str | None = Query(None),
    date_from: date | None = Query(None, alias="from", description="起始日期"),
    date_to: date | None = Query(None, alias="to", description="截止日期"),
    page: Pagination = Depends(get_pagination),
    db: Session = Depends(get_db),
):
    q = db.query(News)
    if keyword:
        like = f"%{keyword}%"
        q = q.filter(or_(News.title.like(like), News.content.like(like)))
    if impact_level:
        q = q.filter(News.impact_level == impact_level)
    if source:
        q = q.filter(News.source == source)
    if date_from:
        q = q.filter(News.published_at >= date_from)
    if date_to:
        q = q.filter(News.published_at <= datetime.combine(date_to, dtime.max))
    total = q.count()
    rows = (q.order_by(News.published_at.desc(), News.id.desc())
            .offset(page.offset).limit(page.limit).all())
    return {
        "items": [{"id": r.id,
                   "published_at": r.published_at.isoformat() if r.published_at else None,
                   "title": r.title, "content": r.content, "source": r.source,
                   "impact_level": r.impact_level, "url": r.url} for r in rows],
        "total": total, "page": page.page, "page_size": page.page_size,
    }
