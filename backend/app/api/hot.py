"""热点题材库接口（T3.4，开发文档 §7 /api/hot_topic/list）。"""
from datetime import date

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.api.deps import get_pagination
from app.models.base import get_db
from app.models.content import HotTopic
from app.schemas.common import Pagination

router = APIRouter(prefix="/api/hot_topic", tags=["hot_topic"])


class HotTopicItem(BaseModel):
    id: int
    date: str
    title: str
    heat: str
    industry_chain: str
    related_codes: list


class HotTopicPage(BaseModel):
    items: list[HotTopicItem]
    total: int
    page: int
    page_size: int


@router.get("/list", response_model=HotTopicPage)
def hot_topic_list(
    keyword: str | None = Query(None, description="标题/产业链模糊匹配"),
    heat: str | None = Query(None, description="高/中/低"),
    on_date: date | None = Query(None, alias="date", description="指定日期"),
    page: Pagination = Depends(get_pagination),
    db: Session = Depends(get_db),
):
    q = db.query(HotTopic)
    if keyword:
        like = f"%{keyword}%"
        q = q.filter(or_(HotTopic.title.like(like), HotTopic.industry_chain.like(like)))
    if heat:
        q = q.filter(HotTopic.heat == heat)
    if on_date:
        q = q.filter(HotTopic.date == on_date)
    total = q.count()
    rows = (q.order_by(HotTopic.date.desc(), HotTopic.id.desc())
            .offset(page.offset).limit(page.limit).all())
    return {
        "items": [{"id": r.id, "date": r.date.isoformat(), "title": r.title,
                   "heat": r.heat, "industry_chain": r.industry_chain,
                   "related_codes": r.related_codes or []} for r in rows],
        "total": total, "page": page.page, "page_size": page.page_size,
    }
