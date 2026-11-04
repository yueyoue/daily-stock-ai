"""APScheduler 任务注册表（T3.6，开发文档 §4.9 调度时间线）。

- 注册表声明式：cron 固定值 或 从系统配置读取（cron_key → config_store）
- build_scheduler：按注册表装配调度器（TZ=Asia/Shanghai），任务本体统一走 run_task（重试/告警）
- report_morning / report_afternoon 为 T7.5 预留位（enabled=False，函数待接线）
"""
import importlib
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.config_store import get_effective
from app.services.task_runner import run_task

logger = logging.getLogger("app.scheduler.registry")

JOB_REGISTRY: list[dict] = [
    dict(
        name="build_indicator_cache",
        func_ref="app.scheduler.indicator_jobs:job_build_indicator_cache",
        cron="0 16 * * 1-5",
        source="fixed",
        enabled=True,
        description="交易日 16:00 指标缓存批处理（§4.9）",
    ),
    dict(
        name="report_morning",
        func_ref="",  # T7.5 接线 app.services.report:job_generate_morning
        cron_key="report_am_cron",
        source="config",
        enabled=False,
        description="盘前早报 08:00（T7.5 接线）",
    ),
    dict(
        name="report_afternoon",
        func_ref="",  # T7.5 接线 app.services.report:job_generate_afternoon
        cron_key="report_pm_cron",
        source="config",
        enabled=False,
        description="盘后复盘 17:00（T7.5 接线）",
    ),
]


def _resolve_cron(db: Session, spec: dict) -> str:
    if spec["source"] == "config":
        return str(get_effective(db, spec["cron_key"]))
    return spec["cron"]


def get_job_specs(db: Session) -> list[dict]:
    """注册表视图（含解析后的 cron，供任务监控/配置页展示）。"""
    out = []
    for spec in JOB_REGISTRY:
        out.append({
            "name": spec["name"],
            "cron": _resolve_cron(db, spec),
            "source": spec["source"],
            "enabled": spec["enabled"],
            "description": spec["description"],
        })
    return out


def _load_func(func_ref: str):
    mod_name, _, attr = func_ref.partition(":")
    return getattr(importlib.import_module(mod_name), attr)


def build_scheduler(db: Session) -> BackgroundScheduler:
    """按注册表装配调度器；任务本体统一包 run_task（重试/告警/task_log）。"""
    scheduler = BackgroundScheduler(timezone=get_settings().timezone)
    for spec in JOB_REGISTRY:
        if not spec["enabled"] or not spec["func_ref"]:
            logger.info("跳过未启用/未接线任务: %s", spec["name"])
            continue
        func = _load_func(spec["func_ref"])
        cron = _resolve_cron(db, spec)
        parts = cron.split()
        scheduler.add_job(
            run_task,
            trigger="cron",
            minute=parts[0], hour=parts[1], day=parts[2], month=parts[3], day_of_week=parts[4],
            id=spec["name"],
            args=[spec["name"], func],
            replace_existing=True,
        )
        logger.info("注册定时任务 %s cron=%s", spec["name"], cron)
    return scheduler
