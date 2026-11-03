"""T3.4 验收：热点 / 资讯 / 观察池 / 任务 API（CRUD + 过滤 + 分页）。"""
import os
from datetime import date, datetime, timedelta

from app.models.base import SessionLocal, init_db
from app.models.content import HotTopic, News, WatchList
from app.models.system import TaskLog

D = date(2026, 11, 3)


def setup_function():
    init_db()


def _clean(db):
    for model in (HotTopic, News, WatchList, TaskLog):
        db.query(model).delete()
    db.commit()


def _login(client):
    r = client.post("/api/auth/login",
                    json={"username": "admin",
                          "password": os.environ["ADMIN_INITIAL_PASSWORD"]})
    assert r.status_code == 200


# ---------- 热点 ----------

def test_hot_topic_list_filters_and_pagination(client):
    db = SessionLocal()
    try:
        _clean(db)
        db.add_all([
            HotTopic(date=D, title="算力基础设施", heat="高",
                     industry_chain="光模块、服务器、IDC", related_codes=["600000"]),
            HotTopic(date=D, title="半导体设备", heat="中",
                     industry_chain="刻蚀、沉积", related_codes=[]),
            HotTopic(date=D - timedelta(days=1), title="消费复苏", heat="低",
                     industry_chain="白酒、零售", related_codes=[]),
        ])
        db.commit()
        _login(client)

        body = client.get("/api/hot_topic/list?page=1&page_size=2").json()
        assert body["total"] == 3 and len(body["items"]) == 2

        kw = client.get("/api/hot_topic/list?keyword=刻蚀").json()
        assert kw["total"] == 1 and kw["items"][0]["title"] == "半导体设备"

        heat = client.get("/api/hot_topic/list?heat=高").json()
        assert heat["total"] == 1 and heat["items"][0]["related_codes"] == ["600000"]

        by_date = client.get(f"/api/hot_topic/list?date={D.isoformat()}").json()
        assert by_date["total"] == 2
    finally:
        db.close()


# ---------- 资讯 ----------

def test_news_list_filters(client):
    db = SessionLocal()
    try:
        _clean(db)
        db.add_all([
            News(published_at=datetime(2026, 11, 3, 7, 21), title="算力产业政策落地",
                 content="支持算力基础设施建设", source="财联社", impact_level="高",
                 dedup_hash="h1"),
            News(published_at=datetime(2026, 11, 3, 9, 15), title="半导体三季报回暖",
                 content="多家设备企业业绩回暖", source="同花顺", impact_level="中",
                 dedup_hash="h2"),
            News(published_at=datetime(2026, 10, 1, 8, 0), title="旧消息",
                 content="无关内容", source="财联社", impact_level="低",
                 dedup_hash="h3"),
        ])
        db.commit()
        _login(client)

        body = client.get("/api/news/list").json()
        assert body["total"] == 3 and body["items"][0]["title"] == "半导体三季报回暖"

        kw = client.get("/api/news/list?keyword=算力").json()
        assert kw["total"] == 1 and kw["items"][0]["impact_level"] == "高"

        lvl = client.get("/api/news/list?impact_level=低").json()
        assert lvl["total"] == 1 and lvl["items"][0]["title"] == "旧消息"

        rng = client.get(f"/api/news/list?from={D.isoformat()}&to={D.isoformat()}").json()
        assert rng["total"] == 2  # 日期区间过滤

        src = client.get("/api/news/list?source=财联社").json()
        assert src["total"] == 2
    finally:
        db.close()


# ---------- 观察池 CRUD ----------

def test_watchlist_crud(client):
    db = SessionLocal()
    try:
        _clean(db)
        _login(client)

        r = client.post("/api/watchlist/add",
                        json={"code": "600000", "name": "演练股", "dimension": "算力"})
        assert r.status_code == 200 and r.json()["code"] == "600000"
        item_id = r.json()["id"]

        dup = client.post("/api/watchlist/add",
                          json={"code": "600000", "dimension": "算力"})
        assert dup.status_code == 409  # 同维度重复入池

        other = client.post("/api/watchlist/add",
                            json={"code": "600000", "dimension": "半导体"})
        assert other.status_code == 200  # 不同维度可并存

        lst = client.get("/api/watchlist/list").json()
        assert lst["total"] == 2
        by_dim = client.get("/api/watchlist/list?dimension=算力").json()
        assert by_dim["total"] == 1

        d = client.delete(f"/api/watchlist/{item_id}")
        assert d.status_code == 200 and d.json()["code"] == "600000"
        assert client.get("/api/watchlist/list").json()["total"] == 1

        assert client.delete("/api/watchlist/9999").json()["code"] == 404
    finally:
        db.close()


# ---------- 任务状态 ----------

def test_task_status_query(client):
    db = SessionLocal()
    try:
        _clean(db)
        db.add_all([
            TaskLog(task_name="采集任务:incremental", status="success",
                    duration_ms=1000, message="ok"),
            TaskLog(task_name="指标缓存批处理", status="failed",
                    duration_ms=500, message="boom"),
            TaskLog(task_name="指标兜底:600000", status="warning",
                    duration_ms=0, message="缺缓存"),
        ])
        db.commit()
        _login(client)

        body = client.get("/api/task/status").json()
        assert body["total"] == 3 and body["items"][0]["task_name"] == "指标兜底:600000"

        failed = client.get("/api/task/status?status=failed").json()
        assert failed["total"] == 1 and failed["items"][0]["message"] == "boom"

        kw = client.get("/api/task/status?keyword=指标").json()
        assert kw["total"] == 2
    finally:
        db.close()


def test_content_apis_require_auth(client):
    for path in ("/api/hot_topic/list", "/api/news/list",
                 "/api/watchlist/list", "/api/task/status"):
        assert client.get(path).status_code == 401, path
