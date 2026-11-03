"""T1.2 验收：交易日历判断 + 缺失兜底。"""
from datetime import date

from app.models.base import SessionLocal, init_db
from app.services.trading_calendar import is_trading_day, upsert_calendar


def setup_function():
    init_db()


def test_calendar_lookup():
    db = SessionLocal()
    try:
        # 2026-11-04 周三=交易日；2026-11-08 周日=非交易日（显式入库）
        upsert_calendar(db, [
            {"date": "2026-11-04", "is_trading_day": True},
            {"date": "2026-11-08", "is_trading_day": False},
        ])
        assert is_trading_day(db, date(2026, 11, 4)) is True
        assert is_trading_day(db, date(2026, 11, 8)) is False
    finally:
        db.close()


def test_missing_entry_falls_back_to_weekday():
    db = SessionLocal()
    try:
        # 未入库的日期：周三兜底 True，周六兜底 False
        assert is_trading_day(db, date(2027, 3, 3)) is True   # 周三
        assert is_trading_day(db, date(2027, 3, 6)) is False  # 周六
    finally:
        db.close()


def test_upsert_updates_existing():
    db = SessionLocal()
    try:
        upsert_calendar(db, [{"date": "2027-05-01", "is_trading_day": True}])
        upsert_calendar(db, [{"date": "2027-05-01", "is_trading_day": False}])  # 劳动节
        assert is_trading_day(db, date(2027, 5, 1)) is False
    finally:
        db.close()
