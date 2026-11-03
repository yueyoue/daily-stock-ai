"""T1.4 验收：降级链（主源失败→备源成功 / 全挂→告警 / 无配置→明确错误）。"""
import pytest

from app.core.errors import AppError
from app.models.base import SessionLocal, init_db
from app.models.system import DataSourceConfig, DataSourceLog
from app.services.collect_chain import collect_with_fallback
from app.services.source_registry import create_source


def setup_function():
    init_db()


def _clean(db):
    db.query(DataSourceConfig).delete()
    db.query(DataSourceLog).delete()
    db.commit()


def test_primary_fail_fallback_succeeds():
    db = SessionLocal()
    try:
        _clean(db)
        create_source(db, data_type="kline", name="坏主源",
                      impl_ref="tests.fake_sources:FakeFailCollector", priority=1)
        create_source(db, data_type="kline", name="好备源",
                      impl_ref="tests.fake_sources:FakeOkCollector", priority=2)

        records = collect_with_fallback(db, "kline")
        assert records == [{"code": "000001", "date": "2026-11-03"}]

        logs = db.query(DataSourceLog).order_by(DataSourceLog.id.asc()).all()
        assert [l.status for l in logs] == ["fail", "ok"]  # 主源失败被记录，备源成功
    finally:
        db.close()


def test_all_sources_fail_raises_502_with_reasons():
    db = SessionLocal()
    try:
        _clean(db)
        create_source(db, data_type="kline", name="坏1",
                      impl_ref="tests.fake_sources:FakeFailCollector", priority=1)
        create_source(db, data_type="kline", name="坏2",
                      impl_ref="tests.fake_sources:FakeFailCollector", priority=2)
        with pytest.raises(AppError) as ei:
            collect_with_fallback(db, "kline")
        assert ei.value.code == 502
        assert "坏1" in ei.value.message and "坏2" in ei.value.message
    finally:
        db.close()


def test_no_config_raises_clear_error():
    db = SessionLocal()
    try:
        _clean(db)
        with pytest.raises(AppError) as ei:
            collect_with_fallback(db, "snapshot")
        assert "无可用接口配置" in ei.value.message
    finally:
        db.close()


def test_params_forwarded_to_collector():
    db = SessionLocal()
    try:
        _clean(db)
        create_source(db, data_type="kline", name="回声源",
                      impl_ref="tests.fake_sources:FakeEchoCollector", priority=1)
        records = collect_with_fallback(db, "kline", code="000001")
        assert records[0]["params"]["code"] == "000001"
    finally:
        db.close()
