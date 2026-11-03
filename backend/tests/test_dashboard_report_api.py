"""T3.2 验收：首页与报告 API（仪表盘聚合 / 报告分页 / 详情 / 重新生成骨架）。

契约以开发文档 §7 为准；LLM 正文生成 T7.4 接入，本任务落结构化骨架。
"""
import os
from datetime import date, timedelta

from app.models.base import SessionLocal, init_db
from app.models.content import HotTopic, Report
from app.models.market import MarketDaily

D = date(2026, 11, 3)


def setup_function():
    init_db()


def _clean(db):
    for model in (Report, HotTopic, MarketDaily):
        db.query(model).delete()
    db.commit()


def _login(client):
    r = client.post("/api/auth/login",
                    json={"username": "admin",
                          "password": os.environ["ADMIN_INITIAL_PASSWORD"]})
    assert r.status_code == 200
    return r


def _seed_market(db):
    for i in range(3):
        d = D - timedelta(days=2 - i)
        db.add(MarketDaily(date=d, up_count=2000 + i, down_count=3000 - i,
                           limit_up_count=30 + i, limit_down_count=5,
                           sentiment_score=60.0 + i, main_moneyflow=1.5e8 + i,
                           extra={"最高连板": 3, "连板梯队": {"1板": 25, "2板": 4, "3板": 1},
                                  "main_sectors": [{"name": "算力", "moneyflow": 1.2e8}]}))
    db.commit()


# ---------- 仪表盘 ----------

def test_dashboard_market_cards_and_trend(client):
    db = SessionLocal()
    try:
        _clean(db)
        _seed_market(db)
        _login(client)

        r = client.get("/api/dashboard/market")
        assert r.status_code == 200
        body = r.json()
        assert body["trade_date"] == D.isoformat()
        assert body["sentiment_score"] == 62.0
        assert body["limit_up_count"] == 32 and body["limit_down_count"] == 5
        assert body["max_consecutive"] == 3
        assert body["consecutive_ladder"] == {"1板": 25, "2板": 4, "3板": 1}
        assert body["main_moneyflow"] == 1.5e8 + 2
        assert body["up_count"] == 2002 and body["down_count"] == 2998
        assert [t["date"] for t in body["sentiment_trend"]] == [
            (D - timedelta(days=2)).isoformat(), (D - timedelta(days=1)).isoformat(),
            D.isoformat()]
        sectors = body["main_sectors"]
        assert [s["name"] for s in sectors] == ["算力"]
        assert sectors[0]["moneyflow"] == 1.2e8

        r2 = client.get("/api/dashboard/market?days=5")
        assert r2.status_code == 200 and len(r2.json()["sentiment_trend"]) == 3
    finally:
        db.close()


def test_dashboard_empty_state_and_auth(client):
    db = SessionLocal()
    try:
        _clean(db)
        assert client.get("/api/dashboard/market").status_code == 401  # 统一鉴权
        _login(client)
        body = client.get("/api/dashboard/market").json()
        assert body["trade_date"] is None and body["sentiment_trend"] == []
        assert body["main_sectors"] == []  # 空数据有占位结构
    finally:
        db.close()


# ---------- 报告 ----------

def test_report_list_pagination_and_filter(client):
    db = SessionLocal()
    try:
        _clean(db)
        for i in range(3):
            db.add(Report(type="morning", report_date=D - timedelta(days=i),
                          title=f"早报{i}", md_content=f"# md{i}", version=1))
        db.add(Report(type="afternoon", report_date=D, title="复盘", md_content="# r"))
        db.commit()
        _login(client)

        body = client.get("/api/report/list?page=1&page_size=2").json()
        assert body["total"] == 4 and body["page"] == 1 and body["page_size"] == 2
        assert len(body["items"]) == 2

        m = client.get("/api/report/list?type=morning").json()
        assert m["total"] == 3 and {i["type"] for i in m["items"]} == {"morning"}
        bad = client.get("/api/report/list?type=bad")
        assert bad.status_code == 400 and bad.json()["code"] == 400
    finally:
        db.close()


def test_report_detail_and_regenerate_versioning(client):
    db = SessionLocal()
    try:
        _clean(db)
        _seed_market(db)
        _login(client)

        r = client.post("/api/report/regenerate",
                        json={"type": "morning", "report_date": D.isoformat()})
        assert r.status_code == 200
        created = r.json()
        assert created["version"] == 1 and created["report_date"] == D.isoformat()

        detail = client.get(f"/api/report/{created['id']}").json()
        assert "速读摘要" in detail["md_content"]  # 7 维度骨架
        for dim in ("隔夜市场大事", "市场情绪预判", "资金动向", "操作提醒",
                    "今日观察方向", "数据日历"):
            assert dim in detail["md_content"], dim

        # 重新生成 → 覆盖旧记录留版本痕迹
        r2 = client.post("/api/report/regenerate",
                         json={"type": "morning", "report_date": D.isoformat()})
        assert r2.json()["id"] == created["id"] and r2.json()["version"] == 2

        # 复盘骨架含情绪/梯队要素
        pm = client.post("/api/report/regenerate",
                         json={"type": "afternoon", "report_date": D.isoformat()}).json()
        pm_md = client.get(f"/api/report/{pm['id']}").json()["md_content"]
        assert "市场情绪得分" in pm_md and "涨停梯队" in pm_md and "策略成功率" in pm_md

        assert client.get("/api/report/9999").json()["code"] == 404  # 404 统一信封
    finally:
        db.close()
