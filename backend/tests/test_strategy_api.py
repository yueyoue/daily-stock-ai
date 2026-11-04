"""T5.3 验收：策略保存与管理（CRUD/重名 409/非法条件 400/保存后加载执行结果一致）。"""
import os
from datetime import date, timedelta

from app.models.base import SessionLocal, init_db
from app.models.market import StockKline
from app.models.system import StockStrategy

D = date(2026, 11, 3)


def setup_function():
    init_db()


def teardown_function():
    """共享测试库：清理策略与行情，避免污染其他模块。"""
    init_db()
    db = SessionLocal()
    try:
        db.query(StockStrategy).delete()
        db.query(StockKline).delete()
        db.commit()
    finally:
        db.close()


def _login(client):
    r = client.post("/api/auth/login",
                    json={"username": "admin",
                          "password": os.environ["ADMIN_INITIAL_PASSWORD"]})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _seed_kline(db, code, base=10.0, days=40):
    price = base
    for i in range(days):
        d = D - timedelta(days=days - 1 - i)
        price *= 1.01 if i % 3 else 0.99
        db.add(StockKline(code=code, date=d, open=price, high=price * 1.01,
                          low=price * 0.99, close=price, volume=1000.0 + i,
                          amount=price * 1000, adj_factor=1.0))
    db.commit()


CONDS = [{"factor": "ma5", "op": ">", "value": 5},
         {"factor": "roe", "op": ">=", "value": 0}]


def test_strategy_crud_round_trip(client):
    h = _login(client)
    r = client.post("/api/selector/strategies",
                    json={"name": "稳健成长", "conditions": CONDS}, headers=h)
    assert r.status_code == 200
    sid = r.json()["id"]
    assert r.json()["conditions"] == CONDS  # 存→取 往返一致

    lst = client.get("/api/selector/strategies", headers=h).json()
    assert lst["total"] == 1 and lst["items"][0]["name"] == "稳健成长"

    up = client.put(f"/api/selector/strategies/{sid}",
                    json={"name": "稳健成长v2",
                          "conditions": [{"factor": "ma5", "op": "between",
                                          "value": 1, "value2": 100}]},
                    headers=h)
    assert up.status_code == 200
    assert up.json()["name"] == "稳健成长v2"
    assert up.json()["conditions"][0]["op"] == "between"

    assert client.delete(f"/api/selector/strategies/{sid}", headers=h).json()["ok"] is True
    assert client.get("/api/selector/strategies", headers=h).json()["total"] == 0


def test_strategy_duplicate_name_409(client):
    h = _login(client)
    assert client.post("/api/selector/strategies",
                       json={"name": "重名", "conditions": CONDS}, headers=h).status_code == 200
    r = client.post("/api/selector/strategies",
                    json={"name": "重名", "conditions": CONDS}, headers=h)
    assert r.status_code == 409 and "已存在" in r.json()["message"]


def test_strategy_invalid_conditions_400(client):
    h = _login(client)
    r = client.post("/api/selector/strategies",
                    json={"name": "非法", "conditions": [{"factor": "不存在", "op": ">", "value": 1}]},
                    headers=h)
    assert r.status_code == 400 and "未知因子" in r.json()["message"]
    r2 = client.post("/api/selector/strategies",
                     json={"name": "非法2", "conditions": [{"factor": "ma5", "op": "??", "value": 1}]},
                     headers=h)
    assert r2.status_code == 400 and "未知比较符" in r2.json()["message"]


def test_saved_strategy_reruns_with_identical_results(client, tmp_path, monkeypatch):
    """验收口径：保存的策略可再次加载执行且结果一致。"""
    db = SessionLocal()
    try:
        db.query(StockKline).delete()
        db.commit()
        _seed_kline(db, "600000", base=10.0)
        _seed_kline(db, "000001", base=20.0)
        import app.services.indicator_cache as ic
        monkeypatch.setattr(ic, "_default_dir", lambda: tmp_path / "empty")
        h = _login(client)

        conds = [{"factor": "ma5", "op": ">", "value": 15.0}]
        saved = client.post("/api/selector/strategies",
                            json={"name": "高均线", "conditions": conds}, headers=h).json()

        # 直接扫描 vs 从策略加载条件扫描（复用）→ 结果一致
        direct = client.post("/api/selector/scan",
                             json={"conditions": conds, "trade_date": D.isoformat()},
                             headers=h).json()
        loaded = client.get("/api/selector/strategies", headers=h).json()["items"][0]
        reused = client.post("/api/selector/scan",
                             json={"conditions": loaded["conditions"],
                                   "trade_date": D.isoformat()}, headers=h).json()
        assert direct["total"] == reused["total"] == 1
        assert direct["candidates"] == reused["candidates"]
        assert saved["conditions"] == loaded["conditions"]
    finally:
        db.close()
