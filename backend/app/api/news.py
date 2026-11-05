"""资讯接口（T3.4，开发文档 §7 /api/news/*；降噪分级 T6.2）。

过滤条件：板块（关键词模糊）/ 关键词 / 日期区间 / 影响等级 / 来源。

T6.2 降噪口径：
- 列表默认隐藏「无关」资讯（only_relevant=true），显式 impact_level=无关 或
  only_relevant=false 可查看全量（无关消息不删除，只做标记，绝不丢数据）
- POST /api/news/classify 触发未分级资讯的 LLM 分级（失败兜底：保持未分级原样）
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
from app.services.news_classify import DEFAULT_BATCH, classify_pending

router = APIRouter(prefix="/api/news", tags=["news"])

IRRELEVANT = "无关"


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


class ClassifyStats(BaseModel):
    pending: int
    batches: int
    failed_batches: int
    classified: dict[str, int]
    irrelevant: int
    unclassified: int
    last_error: str
    ok: bool


@router.get("/list", response_model=NewsPage)
def news_list(
    keyword: str | None = Query(None, description="标题/内容模糊匹配（含板块名）"),
    impact_level: str | None = Query(None, description="高/中/低/无关/未分级(空串)"),
    source: str | None = Query(None),
    date_from: date | None = Query(None, alias="from", description="起始日期"),
    date_to: date | None = Query(None, alias="to", description="截止日期"),
    only_relevant: bool = Query(True, description="默认隐藏「无关」资讯；false 返回全量"),
    page: Pagination = Depends(get_pagination),
    db: Session = Depends(get_db),
):
    q = db.query(News)
    if keyword:
        like = f"%{keyword}%"
        q = q.filter(or_(News.title.like(like), News.content.like(like)))
    if impact_level:
        q = q.filter(News.impact_level == impact_level)
    elif only_relevant:
        # 降噪：未指定等级时默认不展示「无关」；未分级（空串）仍展示，避免分级前"看不见"
        q = q.filter(News.impact_level != IRRELEVANT)
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


@router.post("/classify", response_model=ClassifyStats)
def news_classify(
    limit: int = Query(50, ge=1, le=500, description="本次最多分级条数"),
    batch_size: int = Query(DEFAULT_BATCH, ge=1, le=50, description="单批条数（prompt 长度控制）"),
    db: Session = Depends(get_db),
):
    """对未分级资讯做 LLM 降噪分级（高/中/低/无关）。

    失败兜底：LLM 不可用或输出无法解析 → 对应批次保持「未分级」原样，不写入任何变更，
    仅记 task_log 与统计，**绝不因 LLM 故障丢数据**。
    """
    return classify_pending(db, limit=limit, batch_size=batch_size)