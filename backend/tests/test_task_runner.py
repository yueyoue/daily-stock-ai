"""T3.6 验收：失败自动重试（3 次指数退避）+ 全败告警 + task_log 全链路 + 调度注册表。"""
from app.models.base import SessionLocal, init_db
from app.models.config import SystemConfig
from app.models.system import TaskLog
from app.scheduler.registry import get_job_specs
from app.services.config_store import save_config
from app.services.task_runner import run_task


def _clean(db):
    db.query(TaskLog).delete()
    db.query(SystemConfig).delete()
    db.commit()


def teardown_function():
    """用例共享同一 DB：清理任务日志与配置覆盖，避免污染后续模块。"""
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
    finally:
        db.close()


def test_run_task_success_first_try():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
    finally:
        db.close()

    calls = []

    def ok(db, **kw):
        calls.append(1)

    tid = run_task("测试任务:成功", ok)
    row = SessionLocal().get(TaskLog, tid)
    try:
        assert row.status == "success"
        assert "尝试成功" in row.message
        assert len(calls) == 1
    finally:
        pass


def test_run_task_retries_then_succeeds():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
    finally:
        db.close()

    calls, sleeps = [], []

    def flaky(db, **kw):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("临时故障")

    tid = run_task("测试任务:重试成功", flaky, sleep=lambda s: sleeps.append(s))
    row = SessionLocal().get(TaskLog, tid)
    assert row.status == "success"
    assert "3/3" in row.message          # 第 3 次成功
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]          # 指数退避 1s → 2s


def test_run_task_fails_three_times_and_alerts():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
    finally:
        db.close()

    calls = []

    def always_fail(db, **kw):
        calls.append(1)
        raise RuntimeError("持续故障")

    tid = run_task("测试任务:全败", always_fail, sleep=lambda s: None)
    db = SessionLocal()
    try:
        row = db.get(TaskLog, tid)
        assert row.status == "failed"
        assert "3 次尝试均失败" in row.message
        assert len(calls) == 3            # 共尝试 3 次

        warns = db.query(TaskLog).filter(TaskLog.status == "warning").all()
        assert any(w.task_name.startswith("告警:") for w in warns)   # 告警留痕
        assert any("持续故障" in w.message for w in warns)
    finally:
        db.close()


def test_job_specs_resolve_cron_from_config():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        specs = {s["name"]: s for s in get_job_specs(db)}
        assert specs["build_indicator_cache"]["cron"] == "0 16 * * mon-fri"
        assert specs["report_morning"]["enabled"] is True    # T7.5 已接线（开关走配置）
        assert specs["report_morning"]["cron"] == "0 8 * * mon-fri"

        # 配置页改 cron → 注册表解析立即生效
        save_config(db, report_am_cron="30 7 * * mon-fri")
        specs2 = {s["name"]: s for s in get_job_specs(db)}
        assert specs2["report_morning"]["cron"] == "30 7 * * mon-fri"
    finally:
        db.close()
