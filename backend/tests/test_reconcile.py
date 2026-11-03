"""T1.5 验收：多数一致优先 / 分歧取主源 / 差异留痕。"""
from app.models.base import SessionLocal, init_db
from app.models.system import DataReconcile
from app.services.reconcile import reconcile


def setup_function():
    init_db()
    db = SessionLocal()
    try:
        db.query(DataReconcile).delete()
        db.commit()
    finally:
        db.close()


def test_majority_wins():
    db = SessionLocal()
    try:
        result = reconcile(
            db, "kline", "000001.close@2026-10-31",
            {"mootdx": 12.5, "tencent": 12.5, "eastmoney": 12.6},
            primary="mootdx",
        )
        assert result == 12.5
        row = db.query(DataReconcile).order_by(DataReconcile.id.desc()).first()
        assert row.is_consistent is False  # 有差异
    finally:
        db.close()


def test_no_majority_falls_back_to_primary():
    db = SessionLocal()
    try:
        result = reconcile(
            db, "kline", "000002.close@2026-10-31",
            {"mootdx": 8.8, "tencent": 8.9},
            primary="mootdx",
        )
        assert result == 8.8  # 两源分歧 → 主源为准
    finally:
        db.close()


def test_all_equal_is_consistent():
    db = SessionLocal()
    try:
        result = reconcile(
            db, "kline", "000003.close@2026-10-31",
            {"mootdx": 5.5, "tencent": 5.5},
            primary="mootdx",
        )
        assert result == 5.5
        row = db.query(DataReconcile).order_by(DataReconcile.id.desc()).first()
        assert row.is_consistent is True
    finally:
        db.close()
