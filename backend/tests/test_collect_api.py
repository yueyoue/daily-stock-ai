"""T1.10 验收：POST /api/collect/trigger（鉴权/任务 id/异步执行/task_log 记录）。"""
from datetime import date

from app.models.base import SessionLocal, init_db
from app.models.market import StockInfo, StockKline
from app.models.system import DataSourceConfig, TaskLog
from app.services.source_registry import create_source

from .fake_sources import FakeKlineCollector

ADMIN_USER = "admin"
ADMIN_PASS = "test-pass-123"


def setup_function():
    init_db()


def _clean(db):
    for model in (StockKline, StockInfo, DataSourceConfig, TaskLog):
        db.query(model).delete()
    db.commit()


def _login(client):
    r = client.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert r.status_code == 200
    return r.json()["token"]


def test_trigger_requires_login(client):
    r = client.post("/api/collect/trigger", json={"mode": "incremental"})
    assert r.status_code == 401 and r.json()["code"] == 401


def test_trigger_rejects_invalid_mode_and_type(client):
    token = _login(client)
    headers = {"Authorization": f"Bearer {token}"}
    r = client.post("/api/collect/trigger", json={"mode": "bad"}, headers=headers)
    assert r.status_code == 400
    r = client.post("/api/collect/trigger", json={"mode": "single", "data_type": "bad"},
                    headers=headers)
    assert r.status_code == 400
    r = client.post("/api/collect/trigger", json={"mode": "full", "data_type": "kline"},
                    headers=headers)
    assert r.status_code == 400  # data_type 仅 single 模式有效


def test_trigger_returns_task_id_and_runs_async(client):
    db = SessionLocal()
    try:
        _clean(db)
        FakeKlineCollector.reset()
        db.add(StockInfo(code="600000", name="浦发银行", list_date=date(2020, 1, 1)))
        db.commit()
        create_source(db, data_type="kline", name="假K线源",
                      impl_ref="tests.fake_sources:FakeKlineCollector", priority=1)

        token = _login(client)
        headers = {"Authorization": f"Bearer {token}"}
        r = client.post("/api/collect/trigger",
                        json={"mode": "single", "data_type": "kline"}, headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "queued" and body["task_id"] > 0

        # TestClient 在响应后同步执行后台任务 → 任务行应已跑完
        top = db.get(TaskLog, body["task_id"])
        db.refresh(top)
        assert top.status == "success"
        assert db.query(StockKline).count() == 10  # 后台任务已入库
    finally:
        db.close()
