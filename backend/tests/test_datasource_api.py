"""T3.5 验收：数据源接口 CRUD / 连通测试 / 状态（CRUD 后选路立即生效）。"""
import os

from app.models.base import SessionLocal, init_db
from app.models.system import DataSourceConfig, DataSourceLog


def _login(client):
    r = client.post("/api/auth/login",
                    json={"username": "admin",
                          "password": os.environ["ADMIN_INITIAL_PASSWORD"]})
    assert r.status_code == 200


def _clean(db):
    db.query(DataSourceConfig).delete()
    db.query(DataSourceLog).delete()
    db.commit()


def test_interfaces_crud_lifecycle(client):
    init_db()
    _login(client)
    db = SessionLocal()
    try:
        _clean(db)
    finally:
        db.close()

    # 新增
    r = client.post("/api/datasource/interfaces", json={
        "data_type": "kline", "name": "测试主源",
        "impl_ref": "tests.fake_sources:FakeOkCollector",
        "role": "主源", "priority": 1,
    })
    assert r.status_code == 200
    sid = r.json()["id"]

    # 列表（按类型过滤）
    r2 = client.get("/api/datasource/interfaces", params={"data_type": "kline"})
    assert r2.json()["total"] == 1

    # 重名 → 409
    r3 = client.post("/api/datasource/interfaces", json={
        "data_type": "kline", "name": "测试主源",
        "impl_ref": "tests.fake_sources:FakeOkCollector"})
    assert r3.status_code == 409

    # 修改（启停 + 优先级）
    r4 = client.put(f"/api/datasource/interfaces/{sid}", json={"enabled": False, "priority": 5})
    assert r4.status_code == 200
    assert r4.json()["enabled"] is False and r4.json()["priority"] == 5

    # 选路立即生效：停用后不再是当前选路
    items = client.get("/api/datasource/status").json()["items"]
    item = [i for i in items if i["id"] == sid][0]
    assert item["is_active"] is False

    # 删除
    r5 = client.delete(f"/api/datasource/interfaces/{sid}")
    assert r5.status_code == 200
    assert client.get("/api/datasource/interfaces").json()["total"] == 0


def test_interface_probe_ok_and_fail(client):
    init_db()
    _login(client)
    db = SessionLocal()
    try:
        _clean(db)
    finally:
        db.close()

    ok = client.post("/api/datasource/interfaces", json={
        "data_type": "kline", "name": "好源",
        "impl_ref": "tests.fake_sources:FakeOkCollector"}).json()
    bad = client.post("/api/datasource/interfaces", json={
        "data_type": "kline", "name": "坏源",
        "impl_ref": "tests.fake_sources:FakeFailCollector"}).json()

    # 连通测试：返回结果与耗时
    r = client.post(f"/api/datasource/interfaces/{ok['id']}/test")
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["count"] == 1
    assert r.json()["duration_ms"] >= 0

    r2 = client.post(f"/api/datasource/interfaces/{bad['id']}/test")
    assert r2.json()["ok"] is False
    assert "模拟接口失效" in r2.json()["error"]

    # 状态页反映健康信息
    items = client.get("/api/datasource/status").json()["items"]
    ok_item = [i for i in items if i["id"] == ok["id"]][0]
    bad_item = [i for i in items if i["id"] == bad["id"]][0]
    assert ok_item["last_ok_at"] is not None
    assert "模拟接口失效" in bad_item["last_error"]


def test_status_shows_active_route(client):
    init_db()
    _login(client)
    db = SessionLocal()
    try:
        _clean(db)
    finally:
        db.close()

    client.post("/api/datasource/interfaces", json={
        "data_type": "snapshot", "name": "主", "impl_ref": "tests.fake_sources:FakeOkCollector",
        "role": "主源", "priority": 1})
    client.post("/api/datasource/interfaces", json={
        "data_type": "snapshot", "name": "备", "impl_ref": "tests.fake_sources:FakeOkCollector",
        "role": "备用", "priority": 2})

    items = client.get("/api/datasource/status").json()["items"]
    snap = {i["name"]: i for i in items if i["data_type"] == "snapshot"}
    assert snap["主"]["is_active"] is True   # 优先级高（数字小）者为当前选路
    assert snap["备"]["is_active"] is False
