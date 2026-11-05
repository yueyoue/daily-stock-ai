"""APScheduler 任务注册表（T3.6 建立 / T7.5 接线，开发文档 §4.9 调度时间线）。

- 注册表声明式：cron 固定值 或 从系统配置读取（cron_key → config_store）；
  开关同理（enabled_key → config_store，未声明则取注册表 enabled）
- **交易日前置判断**：trading_only=True 的任务在非交易日不执行任务本体，
  仅写 task_log(status=skipped)。判断必须在 run_task 之外——否则非交易日
  会被统一执行器判为失败并触发 3 次重试与告警。（§4.9 注：非交易日只跑
  资讯线和巡检，不生成报告）
- run_job：注册表任务统一入口（交易日判断 + 统一执行器 run_task：重试/告警/日志）
- build_scheduler：按注册表装配调度器（TZ=Asia/Shanghai），起停由 app.main lifespan 管理
- tz_selfcheck：启动自检 TZ 是否 Asia/Shanghai（§4.9 / §8 硬约束）

⚠️ 星期段写法（T7.6 定时核对修正）：APScheduler 3.x 的 day_of_week 以 0=周一，
与 crontab 的 0=周日不一致（官方 issue 286，v4 才修正）。因此周期性的"工作日"
必须写 `mon-fri`（或 0-4），**不可写 1-5**——在 3.x 下 `1-5` 表示周二~周六，
会导致周一不触发、周六空跑（周六被交易日前置判断跳过，周一则被漏掉）。
"""
from __future__ import annotations

import functools
import importlib
import logging
from datetime import date, datetime

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.base import SessionLocal
from app.services.config_store import get_effective
from app.services.pipeline import log_stage
from app.services.task_runner import run_task
from app.services.trading_calendar import is_trading_day

logger = logging.getLogger("app.scheduler.registry")

# 调度总表（开发文档 §4.9，时区 Asia/Shanghai）
JOB_REGISTRY: list[dict] = [
    dict(
        name="collect_overnight",
        func_ref="app.scheduler.collect_jobs:job_collect_overnight",
        cron_key="collect_am_cron",
        enabled_key="collect_am_enabled",
        source="config",
        enabled=True,
        trading_only=False,  # 资讯线：非交易日仍抓资讯（§4.9 注）
        description="每日 07:30 隔夜资讯采集（为 08:00 早报供料；非交易日仍抓资讯）",
    ),
    dict(
        name="report_morning",
        func_ref="app.services.report:job_generate_morning",
        cron_key="report_am_cron",
        enabled_key="report_am_enabled",
        source="config",
        enabled=True,
        trading_only=True,
        description="交易日 08:00 盘前早报（§4.9）",
    ),
    dict(
        name="collect_close",
        func_ref="app.scheduler.collect_jobs:job_collect_close",
        cron_key="collect_pm_cron",
        enabled_key="collect_pm_enabled",
        source="config",
        enabled=True,
        trading_only=True,
        description="交易日 15:30 收盘数据采集（行情/资金/涨停，§4.9）",
    ),
    dict(
        name="build_indicator_cache",
        func_ref="app.scheduler.indicator_jobs:job_build_indicator_cache",
        cron="0 16 * * mon-fri",
        source="fixed",
        enabled=True,
        trading_only=True,
        description="交易日 16:00 指标缓存批处理（§4.9）",
    ),
    dict(
        name="report_afternoon",
        func_ref="app.services.report:job_generate_afternoon",
        cron_key="report_pm_cron",
        enabled_key="report_pm_enabled",
        source="config",
        enabled=True,
        trading_only=True,
        description="交易日 17:00 盘后复盘（§4.9）",
    ),
]


def _resolve_cron(db: Session, spec: dict) -> str:
    if spec.get("source") == "config":
        return str(get_effective(db, spec["cron_key"]))
    return spec["cron"]


def _is_enabled(db: Session, spec: dict) -> bool:
    """任务开关：配置覆盖层优先（enabled_key），否则取注册表声明。"""
    key = spec.get("enabled_key")
    if key:
        return bool(get_effective(db, key))
    return bool(spec["enabled"])


def get_job_specs(db: Session) -> list[dict]:
    """注册表视图（含解析后的 cron 与开关，供任务监控/配置页展示）。"""
    out = []
    for spec in JOB_REGISTRY:
        out.append({
            "name": spec["name"],
            "cron": _resolve_cron(db, spec),
            "source": spec["source"],
            "enabled": _is_enabled(db, spec),
            "trading_only": bool(spec.get("trading_only", False)),
            "func_ref": spec.get("func_ref", ""),
            "description": spec["description"],
        })
    return out


def _load_func(func_ref: str):
    mod_name, _, attr = func_ref.partition(":")
    return getattr(importlib.import_module(mod_name), attr)


def run_job(spec: dict, *, today: date | None = None, runner=None,
            db: Session | None = None) -> dict:
    """注册表任务统一入口：交易日前置判断 + 统一执行器（重试/告警/task_log）。

    - trading_only=True 且当日非交易日 → 不执行任务本体，只写 task_log(status=skipped)，
      返回 {"executed": False, "reason": "non_trading_day"}（不重试、不告警）
    - 其余 → runner(name, _load_func(func_ref))，默认 run_task（3 次退避重试 + 全败告警）
    runner / today / db 可注入，便于测试与手工补跑。

    返回 {job, executed, date, reason?, task_id?}
    """
    name = spec["name"]
    own = db is None
    db = db or SessionLocal()
    try:
        day = today or date.today()
        if spec.get("trading_only") and not is_trading_day(db, day):
            log_stage(db, f"{name}:跳过"[:64], "skipped",
                      message=f"date={day.isoformat()} 非交易日，按 §4.9 前置判断跳过")
            logger.info("任务 %s 跳过：%s 非交易日", name, day)
            return {"job": name, "executed": False,
                    "reason": "non_trading_day", "date": day.isoformat()}

        func = _load_func(spec["func_ref"])
        result = (runner or run_task)(name, func)
        out: dict = {"job": name, "executed": True, "date": day.isoformat()}
        if isinstance(result, int):
            out["task_id"] = result
        return out
    finally:
        if own:
            db.close()


def tz_selfcheck(scheduler=None) -> dict:
    """TZ 自检：调度器与 Settings 时区必须为 Asia/Shanghai（§4.9/§8 硬约束）。

    返回 {timezone, configured, now, ok}；启动时调用并打日志，便于部署排障。
    """
    settings = get_settings()
    tz_obj = getattr(scheduler, "timezone", None) if scheduler is not None else None
    if tz_obj is None:
        tz = settings.timezone
    else:
        tz = getattr(tz_obj, "zone", None) or str(tz_obj)
    ok = settings.timezone == "Asia/Shanghai" and tz == "Asia/Shanghai"
    info = {"timezone": tz, "configured": settings.timezone,
            "now": datetime.now().isoformat(), "ok": ok}
    if ok:
        logger.info("TZ 自检通过：%s", info)
    else:
        logger.error("TZ 自检失败（定时报告 08:00/17:00 将错位）：%s", info)
    return info


def build_scheduler(db: Session) -> BackgroundScheduler:
    """按注册表装配调度器（不启动；start/shutdown 由 app.main lifespan 管理）。

    - 未接线（func_ref 空）或配置关闭的任务不注册
    - 任务体统一包 run_job（交易日前置判断 + run_task 重试/告警）
    """
    scheduler = BackgroundScheduler(timezone=get_settings().timezone)
    for spec in JOB_REGISTRY:
        if not spec.get("func_ref"):
            logger.info("跳过未接线任务: %s", spec["name"])
            continue
        if not _is_enabled(db, spec):
            logger.info("跳过已关闭任务: %s", spec["name"])
            continue
        cron = _resolve_cron(db, spec)
        parts = cron.split()
        if len(parts) != 5:
            logger.error("cron 非法，跳过任务 %s: %r", spec["name"], cron)
            continue
        scheduler.add_job(
            functools.partial(run_job, spec),
            trigger="cron",
            minute=parts[0], hour=parts[1], day=parts[2], month=parts[3], day_of_week=parts[4],
            id=spec["name"],
            replace_existing=True,
        )
        logger.info("注册定时任务 %s cron=%s trading_only=%s",
                    spec["name"], cron, spec.get("trading_only", False))
    return scheduler