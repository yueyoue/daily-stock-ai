"""任务状态接口（T3.4，开发文档 §7 /api/task/status；task_log 查询）。"""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.api.deps import get_pagination
from app.models.base import get_db
from app.models.system import TaskLog
from app.schemas.common import Pagination

router = APIRouter(prefix="/api/task", tags=["task"])


class TaskItem(BaseModel):
    id: int
    task_name: str
    status: str
    duration_ms: int
    message: str
    created_at: str | None = None


class TaskPage(BaseModel):
    items: list[TaskItem]
    total: int
    page: int
    page_size: int


@router.get("/status", response_model=TaskPage)
def task_status(
    keyword: str | None = Query(None, description="任务名模糊匹配"),
    status: str | None = Query(
        None, description="running/success/failed/warning/skipped"
    ),
    page: Pagination = Depends(get_pagination),
    db: Session = Depends(get_db),
):
    q = db.query(TaskLog)
    if keyword:
        q = q.filter(or_(TaskLog.task_name.like(f"%{keyword}%"),
                         TaskLog.message.like(f"%{keyword}%")))
    if status:
        q = q.filter(TaskLog.status == status)
    total = q.count()
    rows = (q.order_by(TaskLog.id.desc())
            .offset(page.offset).limit(page.limit).all())
    return {
        "items": [{"id": r.id, "task_name": r.task_name, "status": r.status,
                   "duration_ms": r.duration_ms, "message": r.message,
                   "created_at": r.created_at.isoformat() if r.created_at else None}
                  for r in rows],
        "total": total, "page": page.page, "page_size": page.page_size,
    }