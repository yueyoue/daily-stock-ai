"""掘金观察池接口（T3.4，开发文档 §7 /api/watchlist/*；一期单用户）。"""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.api.deps import get_pagination
from app.models.base import get_db
from app.models.content import WatchList
from app.schemas.common import Pagination

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class WatchItem(BaseModel):
    id: int
    code: str
    name: str
    dimension: str
    created_at: str | None = None


class WatchPage(BaseModel):
    items: list[WatchItem]
    total: int
    page: int
    page_size: int


class WatchAddRequest(BaseModel):
    code: str = Field(min_length=1, max_length=12)
    name: str = ""
    dimension: str = ""


def _row(r: WatchList) -> dict:
    return {"id": r.id, "code": r.code, "name": r.name, "dimension": r.dimension,
            "created_at": r.created_at.isoformat() if r.created_at else None}


@router.get("/list", response_model=WatchPage)
def watchlist_list(
    dimension: str | None = Query(None, description="关注维度/概念过滤"),
    page: Pagination = Depends(get_pagination),
    db: Session = Depends(get_db),
):
    q = db.query(WatchList)
    if dimension:
        q = q.filter(WatchList.dimension == dimension)
    total = q.count()
    rows = (q.order_by(WatchList.id.desc())
            .offset(page.offset).limit(page.limit).all())
    return {"items": [_row(r) for r in rows], "total": total,
            "page": page.page, "page_size": page.page_size}


@router.post("/add", response_model=WatchItem)
def watchlist_add(payload: WatchAddRequest, db: Session = Depends(get_db)):
    exists = (db.query(WatchList)
              .filter(WatchList.code == payload.code,
                      WatchList.dimension == payload.dimension)
              .first())
    if exists is not None:
        raise AppError(409, "该标的已在此关注维度中")
    row = WatchList(code=payload.code, name=payload.name,
                    dimension=payload.dimension)
    db.add(row)
    db.commit()
    db.refresh(row)
    return _row(row)


@router.delete("/{item_id}", response_model=WatchItem)
def watchlist_delete(item_id: int, db: Session = Depends(get_db)):
    row = db.get(WatchList, item_id)
    if row is None:
        raise AppError(404, "观察池记录不存在")
    data = _row(row)
    db.delete(row)
    db.commit()
    return data
