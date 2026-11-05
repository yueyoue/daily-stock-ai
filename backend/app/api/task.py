"""任务状态接口（T3.4，开发文档 §7 /api/task/status；task_log 查询）。"""
import re

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.api.deps import get_pagination
from app.models.base import get_db
from app.models.system import TaskLog
from app.schemas.common import Pagination

router = APIRouter(prefix="/api/task", tags=["task"])

# D-11 修复：任务详情列不再原样暴露 SQL 语句 / 驱动堆栈 / 文件路径，
# 统一摘要为「单行、可读」的原因文本，避免把底层实现细节泄漏到界面。
_SQL_STMT = re.compile(
    r"(?is)\b(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|ALTER\s+TABLE|CREATE\s+TABLE)\b.*?(?=;|\n|$)"
)
_SQL_BLOCK = re.compile(
    r"(?is)\[(?:SQL|parameters|raw\s+sql|generated\s+sql|cached\s+sql)[^\]]*\]"
)
_BACKGROUND = re.compile(r"(?is)\(Background on this error at:[^)]*\)")
_TRACEBACK = re.compile(
    r'(?is)(?:Traceback \(most recent call last\):|File "[^"]+", line \d+[^\n]*)'
)
_WS = re.compile(r"\s+")


def summarize_message(message: str | None, limit: int = 200) -> str:
    """把 task_log.message 摘要为单行可读文本（去 SQL 块/堆栈/路径，超长截断）。"""
    text = (message or "").strip()
    if not text:
        return ""
    text = _BACKGROUND.sub(" ", text)
    text = _SQL_BLOCK.sub(" ", text)
    text = _TRACEBACK.sub(" ", text)
    text = _SQL_STMT.sub("[SQL 语句已省略] ", text)
    text = _WS.sub(" ", text).strip(" ;,")
    if len(text) > limit:
        text = text[:limit].rstrip() + "…（完整原因见任务日志）"
    return text


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
                   "duration_ms": r.duration_ms,
                   "message": summarize_message(r.message),
                   "created_at": r.created_at.isoformat() if r.created_at else None}
                  for r in rows],
        "total": total, "page": page.page, "page_size": page.page_size,
    }