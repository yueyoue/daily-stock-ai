"""T7.5 验收：定时调度（开发文档 §4.9 调度时间线；进度文档 T7.5）。

覆盖验收点：
- 注册表接线：报告/采集任务 cron 与开关取自系统配置，改配置即时生效
- **非交易日不生成报告**：交易日前置判断在 run_task 之外，非交易日只写
  task_log(status=skipped)，不调用任务本体、不重试不告警
- 交易日执行：走统一执行器（run_task：重试/告警/task_log），LLM 未就绪时落结构化骨架
- 资讯线不受交易日限制（§4.9 注：非交易日只跑资讯线和巡检）
- `TZ` 自检通过（Asia/Shanghai，§8 硬约束）
- build_scheduler 按注册表装配：未接线/已关闭任务不注册，装配不自动 start
"""
from datetime import date

from app.core.errors import AppError
from app.models.base import SessionLocal, init_db
from app.models.config import SystemConfig
from app.models.content import Report
from app.models.system import TaskLog, TradeCalendar
from app.scheduler.registry import (
    JOB_REGISTRY,
    build_scheduler,
    get_job_specs,
    run_job,
    tz_selfcheck,
)
from app.services.config_store import save_config
from app.services.task_runner import run_task

TRADING = date(2026, 11, 5)       # 周四：无日历时按"周一~周五兜底"判为交易日
NON_TRADING = date(2026, 11, 7)   # 周六：非交易日

REGISTRY_NAMES = {
    "collect_overnight", "report_morning", "collect_close",
    "build_indicator_cache", "report_afternoon",
}


def _clean(db):
    # TradeCalendar 必删：全量套件共享同一 SQLite 文件，其他用例写入的日历
    # 会改变 is_trading_day 的判断基准。
    for model in (Report, TaskLog, TradeCalendar, SystemConfig):
        db.query(model).delete()
    db.commit()


def teardown_function():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
    finally:
        db.close()


def _spec(name: str) -> dict:
    return next(s for s in JOB_REGISTRY if s["name"] == name)


# ---------- 注册表接线 ----------

def test_registry_wires_all_jobs_from_config():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        specs = {s["name"]: s for s in get_job_specs(db)}
        assert set(specs) == REGISTRY_NAMES
        assert all(s["func_ref"] for s in specs.values())        # 无未接线任务（T7.5 全部接线）

        assert specs["report_morning"]["func_ref"] == "app.services.report:job_generate_morning"
        assert specs["report_morning"]["enabled"] is True
        assert specs["report_morning"]["trading_only"] is True
        assert specs["report_morning"]["cron"] == "0 8 * * 1-5"

        assert specs["report_afternoon"]["cron"] == "0 17 * * 1-5"
        assert specs["report_afternoon"]["trading_only"] is True

        # 资讯线：每日 07:30 且不受交易日限制；收盘采集：交易日 15:30
        assert specs["collect_overnight"]["cron"] == "30 7 * * *"
        assert specs["collect_overnight"]["trading_only"] is False
        assert specs["collect_close"]["cron"] == "30 15 * * 1-5"
        assert specs["build_indicator_cache"]["cron"] == "0 16 * * 1-5"

        # 配置页改时间/开关 → 注册表解析即时生效
        save_config(db, report_am_cron="30 7 * * 1-5", report_am_enabled=False)
        specs2 = {s["name"]: s for s in get_job_specs(db)}
        assert specs2["report_morning"]["cron"] == "30 7 * * 1-5"
        assert specs2["report_morning"]["enabled"] is False
    finally:
        db.close()


# ---------- 交易日前置判断（验收核心） ----------

def test_non_trading_day_skips_report_without_generating():
    """非交易日不生成报告：不调用任务本体、不进入重试、只留 skipped 日志。"""
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        calls: list[str] = []

        res = run_job(_spec("report_morning"), today=NON_TRADING, db=db,
                      runner=lambda name, fn: calls.append(name))

        assert res["executed"] is False and res["reason"] == "non_trading_day"
        assert res["date"] == NON_TRADING.isoformat()
        assert calls == []                                      # 未调用任务本体（判断在 run_task 之外）
        assert db.query(Report).count() == 0                    # 未生成报告
        assert db.query(TaskLog).filter(
            TaskLog.status.in_(("failed", "warning"))).count() == 0   # 无失败/无告警

        skipped = db.query(TaskLog).filter(TaskLog.status == "skipped").all()
        assert len(skipped) == 1
        assert skipped[0].task_name == "report_morning:跳过"
        assert "非交易日" in skipped[0].message
    finally:
        db.close()


def test_trading_day_runs_report_through_task_runner(monkeypatch):
    """交易日：走统一执行器；LLM 未就绪（503）→ 结构化骨架落库，链路不整体失败。"""
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        import app.services.llm as llm_mod

        def unavailable(*args, **kwargs):
            raise AppError(503, "未配置 LLM API Key（请先在系统设置保存）")

        monkeypatch.setattr(llm_mod, "complete", unavailable)

        specs = {s["name"]: s for s in get_job_specs(db)}
        calls: list[str] = []
        wrapped = lambda name, fn: calls.append(name) or run_task(  # noqa: E731
            name, lambda d, **kw: fn(d, run_date=TRADING)
        )

        res = run_job(specs["report_morning"], today=TRADING, db=db, runner=wrapped)

        assert res["executed"] is True and res["date"] == TRADING.isoformat()
        assert calls == ["report_morning"]
        rows = db.query(Report).all()
        assert len(rows) == 1 and rows[0].type == "morning"
        assert rows[0].report_date == TRADING                   # 生成的是基准日报告
        assert "结构化骨架" in rows[0].md_content               # LLM 未就绪 → 降级骨架
        assert db.query(TaskLog).filter(
            TaskLog.task_name == "report_morning", TaskLog.status == "success").count() == 1
    finally:
        db.close()


def test_news_job_not_gated_by_trading_day():
    """资讯线：非交易日仍执行（§4.9 注：非交易日只跑资讯线和巡检）。"""
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        captured: list[tuple[str, str]] = []

        res = run_job(_spec("collect_overnight"), today=NON_TRADING, db=db,
                      runner=lambda name, fn: captured.append((name, fn.__name__)))

        assert res["executed"] is True and "reason" not in res
        assert captured == [("collect_overnight", "job_collect_overnight")]
        assert db.query(TaskLog).filter(TaskLog.status == "skipped").count() == 0
    finally:
        db.close()


# ---------- TZ 自检 / 调度器装配 ----------

def test_tz_selfcheck_reports_asia_shanghai():
    info = tz_selfcheck()
    assert info["ok"] is True                                   # §8：全部服务 TZ=Asia/Shanghai
    assert info["configured"] == "Asia/Shanghai"
    assert info["timezone"] == "Asia/Shanghai"
    assert info["now"]


def test_build_scheduler_assembles_registry_without_start():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        sched = build_scheduler(db)
        try:
            ids = {j.id for j in sched.get_jobs()}
            assert ids == REGISTRY_NAMES                        # 5 项全部注册
            assert sched.running is False                       # 装配不启动（lifespan 负责 start）
            assert getattr(sched.timezone, "zone", str(sched.timezone)) == "Asia/Shanghai"
        finally:
            pass

        # 配置关闭任务 → 下一次装配不注册该任务
        save_config(db, report_pm_enabled=False, collect_am_enabled=False)
        sched2 = build_scheduler(db)
        ids2 = {j.id for j in sched2.get_jobs()}
        assert "report_afternoon" not in ids2
        assert "collect_overnight" not in ids2
        assert {"report_morning", "collect_close", "build_indicator_cache"} <= ids2
    finally:
        db.close()