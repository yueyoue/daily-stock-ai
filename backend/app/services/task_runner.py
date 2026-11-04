"""统一后台任务执行器（T3.6，开发文档 §1.4：失败自动重试、全链路日志、失败告警）。

- run_task：同步执行 + 3 次指数退避重试 + task_log 全覆盖 + 告警钩子
- submit_background：进程内后台线程执行（独立 DB 会话）
- 告警钩子：ALERT 日志 + task_log warning 行 + webhook 预留（Settings.alert_webhook）
"""
import logging
import threading
import time

from app.models.base import SessionLocal
from app.models.system import TaskLog
from app.services.pipeline import log_stage

logger = logging.getLogger("app.task_runner")

RETRY_ATTEMPTS = 3
BACKOFF_BASE_SEC = 1.0


def alert(task_name: str, message: str, db=None) -> None:
    """失败告警钩子：日志 ALERT + task_log 留痕 + webhook 预留（T7.6 接真）。"""
    logger.error("ALERT [%s] %s", task_name, message)
    own = db is None
    db = db or SessionLocal()
    try:
        log_stage(db, f"告警:{task_name}"[:64], "warning", message=message[:2000])
    finally:
        if own:
            db.close()
    webhook = ""
    try:
        from app.core.config import get_settings

        webhook = getattr(get_settings(), "alert_webhook", "")
    except Exception:  # noqa: BLE001
        pass
    if webhook:
        logger.info("webhook 告警（预留通道，T7.6 接真）: %s", webhook)


def run_task(
    task_name: str,
    fn,
    *,
    task_id: int | None = None,
    attempts: int = RETRY_ATTEMPTS,
    backoff_base: float = BACKOFF_BASE_SEC,
    sleep=time.sleep,
    **fn_kwargs,
) -> int:
    """执行单个后台任务：指数退避重试 + task_log 全链路 + 全败告警。

    fn(db, **fn_kwargs) 为任务本体（异常即失败，触发重试）；task_id 复用已建的顶层日志行。
    返回 task_id。
    """
    db = SessionLocal()
    try:
        top: TaskLog = (
            db.get(TaskLog, task_id) if task_id is not None
            else log_stage(db, task_name[:64], "running")
        )
        last_err: Exception | None = None
        for attempt in range(1, attempts + 1):
            t0 = time.monotonic()
            try:
                fn(db, **fn_kwargs)
                top.status = "success"
                top.duration_ms = int((time.monotonic() - t0) * 1000)
                top.message = (f"{top.message or ''} | 第 {attempt}/{attempts} 次尝试成功".strip(" |"))
                db.commit()
                return top.id
            except Exception as e:  # noqa: BLE001 任务异常统一转重试
                last_err = e
                db.rollback()  # 清理失败事务状态
                logger.warning("任务 %s 第 %d/%d 次失败: %s", task_name, attempt, attempts, e)
                if attempt < attempts:
                    sleep(backoff_base * (2 ** (attempt - 1)))

        top.status = "failed"
        top.message = f"{attempts} 次尝试均失败: {last_err}"[:2000]
        db.commit()
        alert(task_name, f"任务失败（重试 {attempts} 次后放弃）: {last_err}", db=db)
        return top.id
    finally:
        db.close()


def submit_background(task_name: str, fn, *, task_id: int | None = None, **fn_kwargs) -> threading.Thread:
    """进程内后台线程执行（独立会话在 run_task 内部管理）。"""
    thread = threading.Thread(
        target=run_task,
        kwargs={"task_name": task_name, "fn": fn, "task_id": task_id, **fn_kwargs},
        daemon=True,
        name=f"task-{task_name}",
    )
    thread.start()
    return thread
