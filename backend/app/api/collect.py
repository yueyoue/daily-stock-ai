"""采集触发接口（开发文档 §7 /api/collect/trigger，任务 T1.10）。

POST /api/collect/trigger 异步执行采集流水线，立即返回 task_id（task_log 顶层任务行）。
模式：full（全量）/ incremental（日更）/ single（单数据类型）。
任务执行为进程内后台任务（T3.6 将升级为完整任务队列与重试）。
"""
import logging
from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_user
from app.core.errors import AppError
from app.models.admin_user import AdminUser
from app.models.base import SessionLocal, get_db
from app.services.pipeline import MODES, SINGLE_TYPES, log_stage, run_pipeline

logger = logging.getLogger("app.api.collect")

router = APIRouter(prefix="/api/collect", tags=["collect"])


class CollectTriggerRequest(BaseModel):
    mode: str = Field(default="incremental", description="full / incremental / single")
    data_type: str | None = Field(default=None, description="single 模式的数据类型")


def _run_task(task_id: int, mode: str, data_type: str | None) -> None:
    """后台执行：独立会话跑流水线，异常记 task_log 不外抛。"""
    db = SessionLocal()
    try:
        run_pipeline(db, mode, data_type, task_id=task_id)
    except Exception as e:  # noqa: BLE001 后台任务异常统一记日志
        logger.error("采集任务 %s 失败: %s", task_id, e)
    finally:
        db.close()


@router.post("/trigger")
def trigger_collect(
    payload: CollectTriggerRequest,
    background_tasks: BackgroundTasks,
    user: AdminUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    mode = payload.mode.strip()
    if mode not in MODES:
        raise AppError(400, f"mode 需为 {MODES} 之一")
    if mode == "single":
        if payload.data_type not in SINGLE_TYPES:
            raise AppError(400, f"single 模式 data_type 需为 {SINGLE_TYPES} 之一")
    elif payload.data_type:
        raise AppError(400, "data_type 仅在 single 模式下有效")

    row = log_stage(
        db,
        f"采集任务:{mode}" + (f":{payload.data_type}" if payload.data_type else ""),
        "queued",
        message=f"触发人={user.username}, date={date.today()}",
    )
    background_tasks.add_task(_run_task, row.id, mode, payload.data_type)
    return {"task_id": row.id, "status": "queued", "mode": mode,
            "data_type": payload.data_type}
