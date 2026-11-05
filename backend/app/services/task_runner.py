"""统一后台任务执行器（T3.6，开发文档 §1.4：失败自动重试、全链路日志、失败告警）。

- run_task：同步执行 + 3 次指数退避重试 + task_log 全覆盖 + 告警钩子
- submit_background：进程内后台线程执行（独立 DB 会话）
- alert：ALERT 日志 + task_log warning 行 + webhook 投递（T7.6 接真，
  URL 取 config_store.get_effective("alert_webhook")，即"系统配置页 > .env > 默认"；
  3 秒超时、best-effort，投递失败只 warning，绝不影响任务本身与 task_log 留痕）
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from app.models.base import SessionLocal
from app.models.system import TaskLog
from app.services.pipeline import log_stage

logger = logging.getLogger("app.task_runner")

RETRY_ATTEMPTS = 3
BACKOFF_BASE_SEC = 1.0
WEBHOOK_TIMEOUT_SEC = 3.0


def _resolve_webhook(db=None) -> str:
    """解析生效的告警 webhook 地址：系统配置（覆盖层）> .env > 默认（空）。

    失败一律回退空串（无告警通道），不向上抛异常。
    """
    try:
        if db is not None:
            from app.services.config_store import get_effective

            value = get_effective(db, "alert_webhook")
            if value:
                return str(value).strip()
        from app.core.config import get_settings

        return str(getattr(get_settings(), "alert_webhook", "") or "").strip()
    except Exception:  # noqa: BLE001 告警通道解析失败不得影响任务
        logger.warning("告警 webhook 解析失败，本次跳过 webhook 投递", exc_info=True)
        return ""


def _post_webhook(url: str, payload: dict) -> bool:
    """向 webhook POST JSON（best-effort）：成功返回 True；非 2xx/超时/异常只记 warning。

    失败绝不抛出：告警通道故障不允许影响任务本体与 task_log 留痕。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT_SEC) as resp:  # noqa: S310 仅告警通道
            code = getattr(resp, "status", None) or resp.getcode()
            if 200 <= int(code) < 300:
                return True
            logger.warning("webhook 告警返回非 2xx：%s %s", code, url)
            return False
    except Exception as e:  # noqa: BLE001 网络/超时/编码等一律降级为 warning
        logger.warning("webhook 告警投递失败（%s）：%s", url, e)
        return False


def alert(task_name: str, message: str, db=None) -> bool:
    """失败告警钩子：日志 ALERT + task_log 留痕 + webhook 投递（T7.6 接真）。

    返回 webhook 是否投递成功（无通道/未配置时返回 False，不影响调用方）。
    """
    logger.error("ALERT [%s] %s", task_name, message)
    own = db is None
    db = db or SessionLocal()
    webhook = ""
    try:
        # 必须先解析 webhook：db 可能由本函数自建、随后即关（T7.6 接真的关键顺序）
        webhook = _resolve_webhook(db)
        log_stage(db, f"告警:{task_name}"[:64], "warning", message=message[:2000])
    finally:
        if own:
            db.close()
    if not webhook:
        return False
    payload = {
        "source": "daily-stock-ai",
        "level": "error",
        "task": task_name,
        "message": message,
        "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
    }
    return _post_webhook(webhook, payload)


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