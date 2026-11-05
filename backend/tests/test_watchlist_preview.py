"""T6.5 验收（CI 可测部分）：观察池快照预览（行情/涨跌幅/ROE/概念）、API 字段下发。"""
from datetime import date

from app.models.base import SessionLocal, init_db
from app.models.content import WatchList
from app.models.market import StockFinIndicator, StockInfo, StockKline
from app.services.watchlist import preview_for_codes

D1 = date(2026, 11, 4)
D2 = date(2026, 11, 5)


def _clean(db):
    for model in (WatchList, StockKline, StockFinIndicator, StockInfo):
        db.query(model).delete()
    db.commit()


def _seed(db):
    db.add_all([
        StockInfo(code="000001", name="平安银行", industry="银行",
                  concepts=["金融科技", "银行"]),
        StockKline(code="000001", date=D1, open=10, high=10.5, low=9.8, close=10.0),
        StockKline(code="000001", date=D2, open=10.1, high=10.6, low=10.0, close=10.5),
        StockFinIndicator(code="000001", report_date=date(2026, 9, 30), roe=12.6),
    ])
    db.commit()


def test_preview_for_codes():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        _seed(db)
        preview = preview_for_codes(db, ["000001", "999999"])

        p = preview["000001"]
        assert p["latest_date"] == "2026-11-05"
        assert p["close"] == 10.5
        assert p["pct_change"] == 5.0        # (10.5/10.0-1)*100
        assert p["roe"] == 12.6
        assert p["industry"] == "银行"
        assert p["concepts"] == ["金融科技", "银行"]
        assert p["market_cap"] is None       # 待快照线扩展

        empty = preview["999999"]            # 无数据标的 → 全空占位
        assert empty["close"] is None and empty["roe"] is None
    finally:
        _clean(db)
        db.close()


def test_preview_handles_single_kline_row():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        db.add(StockKline(code="600519", date=D2, open=1, high=1, low=1, close=5))
        db.commit()
        p = preview_for_codes(db, ["600519"])["600519"]
        assert p["close"] == 5
        assert p["pct_change"] is None       # 仅一天 → 不算涨跌幅
    finally:
        _clean(db)
        db.close()


def test_watchlist_api_includes_preview(client):
    """列表接口下发预览字段（additive，不破坏 T3.4 契约）。"""
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        _seed(db)
        db.add(WatchList(code="000001", name="平安银行", dimension="低估值"))
        db.commit()
    finally:
        db.close()

    r = client.post("/api/auth/login", json={
        "username": "admin",
        "password": __import__("os").environ["ADMIN_INITIAL_PASSWORD"]})
    assert r.status_code == 200

    r2 = client.get("/api/watchlist/list")
    assert r2.status_code == 200
    item = r2.json()["items"][0]
    assert item["code"] == "000001"
    assert item["close"] == 10.5
    assert item["roe"] == 12.6
    assert item["concepts"] == ["金融科技", "银行"]
    assert item["pct_change"] == 5.0
