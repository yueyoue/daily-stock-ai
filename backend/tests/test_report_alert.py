"""T7.6 验收（其一）：任务失败重试 + 告警接真（开发文档 §1.4「任务失败自动重试、全链路日志留存」）。

覆盖验收点：
- 告警通道地址取 `config_store.get_effective("alert_webhook")`：系统配置页可改且改后立即生效
- 配了 webhook 就真实投递：本地 http.server 收到 POST JSON，payload 带 task / message / level
- webhook 不可达（连接被拒）：只 warning，不抛异常，不影响 task_log 留痕与任务结果
- 报告任务 3 次重试仍失败：task_log(status=failed) + 告警 warning 行 + webhook 投递
"""
from __future__ import annotations

import contextlib
import http.server
import json as jsonlib
import threading
from datetime import date

from app.core.errors import AppError
from app.models.base import SessionLocal, init_db
from app.models.config import SystemConfig
from app.models.system import TaskLog, TradeCalendar
from app.services.config_store import get_effective, save_config
from app.services.report import job_generate_morning
from app.services.task_runner import RETRY_ATTEMPTS, alert, run_task

TRADING = date(2026, 11, 5)  # 周四：无日历时按"周一~周五兜底"判为交易日
UNREACHABLE = "http://127.0.0.1:1/alert"  # 端口 1：连接必被拒（不会有人监听）

RECEIVED: list[dict] = []


class _WebhookHandler(http.server.BaseHTTPRequestHandler):
    """测试桩：把收到的 POST JSON 记进 RECEIVED，回 200。"""

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            RECEIVED.append(jsonlib.loads(raw.decode("utf-8")))
        except Exception:  # noqa: BLE001 测试桩：非 JSON 原样记录
            RECEIVED.append({"raw": raw.decode("utf-8", "replace")})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):  # 静默：测试桩不刷日志
        return


@contextlib.contextmanager
def _webhook_server():
    """起一个随机端口的本地 webhook 服务，yield 出 URL。"""
    srv = http.server.HTTPServer(("127.0.0.1", 0), _WebhookHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    RECEIVED.clear()
    try:
        yield f"http://127.0.0.1:{srv.server_port}/alert"
    finally:
        srv.shutdown()
        srv.server_close()


def _clean(db):
    # TradeCalendar 必删：全量套件共享同一 SQLite 文件，其他用例写入的日历
    # 会改变交易日判断基准；SystemConfig 必删：避免上一条用例的 alert_webhook 残留。
    for model in (TaskLog, TradeCalendar, SystemConfig):
        db.query(model).delete()
    db.commit()


def teardown_function():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
    finally:
        db.close()


def _boom(*args, **kwargs):
    raise AppError(500, "模拟任务失败")


# ---------- 告警通道：配置生效 + 真实投递 ----------

def test_alert_delivers_to_configured_webhook_and_logs_warning():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        with _webhook_server() as url:
            save_config(db, alert_webhook=url)
            assert get_effective(db, "alert_webhook") == url   # 配置页覆盖（> .env > 默认）

            delivered = alert("report_morning", "任务失败（重试 3 次后放弃）：模拟", db=db)

        assert delivered is True                               # 投递成功返回 True
        assert len(RECEIVED) == 1
        payload = RECEIVED[0]
        assert payload["task"] == "report_morning"
        assert "任务失败" in payload["message"]
        assert payload["level"] == "error" and payload["source"] == "daily-stock-ai"
        assert payload["timestamp"]

        warn = (db.query(TaskLog)
                .filter(TaskLog.task_name == "告警:report_morning",
                        TaskLog.status == "warning").all())
        assert len(warn) == 1                                  # task_log 留痕独立于 webhook
        assert "任务失败" in warn[0].message
    finally:
        db.close()


# ---------- 告警通道故障：best-effort，不影响任务与留痕 ----------

def test_alert_survives_unreachable_webhook():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        save_config(db, alert_webhook=UNREACHABLE)

        delivered = alert("demo_task", "任务失败（重试 3 次后放弃）：模拟", db=db)
        assert delivered is False                              # 通道不可用只降级，不抛异常

        warn = (db.query(TaskLog)
                .filter(TaskLog.task_name == "告警:demo_task",
                        TaskLog.status == "warning").all())
        assert len(warn) == 1                                  # 告警留痕仍在

        # 任务本体失败不受告警通道影响：run_task 正常收尾并落 failed 日志
        task_id = run_task("demo_task", _boom, sleep=lambda _s: None)
        db.expire_all()
        top = db.get(TaskLog, task_id)
        assert top.status == "failed"
        assert f"{RETRY_ATTEMPTS} 次尝试均失败" in top.message
    finally:
        db.close()


# ---------- 报告任务失败链路：重试 3 次 → failed + 告警 + webhook ----------

def test_failed_report_task_triggers_alert_and_webhook(monkeypatch):
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        import app.services.llm as llm_mod

        # LLM 输出既无必需维度也无必填数值 → 模板层重试用尽 → 502 → 执行器重试用尽
        monkeypatch.setattr(llm_mod, "complete",
                            lambda *args, **kwargs: "（本次输出不含任何维度标题与数值）")

        with _webhook_server() as url:
            save_config(db, alert_webhook=url)
            task_id = run_task(
                "report_morning",
                lambda db_: job_generate_morning(db_, run_date=TRADING),
                sleep=lambda _s: None,
            )

        db.expire_all()
        top = db.get(TaskLog, task_id)
        assert top.task_name == "report_morning"
        assert top.status == "failed"
        assert f"{RETRY_ATTEMPTS} 次尝试均失败" in top.message

        warn = (db.query(TaskLog)
                .filter(TaskLog.task_name == "告警:report_morning",
                        TaskLog.status == "warning").all())
        assert len(warn) == 1

        assert len(RECEIVED) == 1
        assert RECEIVED[0]["task"] == "report_morning"
        assert "任务失败" in RECEIVED[0]["message"]
    finally:
        db.close()