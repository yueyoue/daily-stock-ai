"""T1.3 验收：选路（启用+优先级）、impl_ref 加载、调用日志、CRUD、连通测试、限流。"""
import pytest

from app.core.errors import AppError
from app.models.base import SessionLocal, init_db
from app.models.system import DataSourceConfig, DataSourceLog
from app.services.source_registry import (
    DataSourceError,
    build_collector,
    call_source,
    create_source,
    delete_source,
    pick_sources,
    probe_source,
    update_source,
)
from app.utils.ratelimit import RateLimiter


def setup_function():
    init_db()


def _clean(db):
    db.query(DataSourceConfig).delete()
    db.query(DataSourceLog).delete()
    db.commit()


def _seed(db):
    _clean(db)
    create_source(
        db, data_type="kline", name="主源", impl_ref="tests.fake_sources:FakeOkCollector",
        role="主源", priority=1, enabled=True,
    )
    create_source(
        db, data_type="kline", name="备源", impl_ref="tests.fake_sources:FakeEchoCollector",
        role="备用", priority=2, enabled=True,
    )
    create_source(
        db, data_type="kline", name="停用源", impl_ref="tests.fake_sources:FakeFailCollector",
        role="备用", priority=0, enabled=False,
    )


def test_pick_sources_by_priority_and_enabled():
    db = SessionLocal()
    try:
        _seed(db)
        sources = pick_sources(db, "kline")
        assert [s.name for s in sources] == ["主源", "备源"]  # 停用源不参与，按 priority 升序
    finally:
        db.close()


def test_build_collector_by_impl_ref():
    db = SessionLocal()
    try:
        _seed(db)
        src = pick_sources(db, "kline")[0]
        collector = build_collector(src)
        assert collector.fetch() == [{"code": "000001", "date": "2026-11-03"}]
    finally:
        db.close()


def test_call_source_ok_writes_log():
    db = SessionLocal()
    try:
        _seed(db)
        src = pick_sources(db, "kline")[0]
        records = call_source(db, src)
        assert len(records) == 1
        log = db.query(DataSourceLog).order_by(DataSourceLog.id.desc()).first()
        assert log.status == "ok" and log.source_name == "主源"
        assert src.last_ok_at is not None
    finally:
        db.close()


def test_call_source_fail_logs_and_raises():
    db = SessionLocal()
    try:
        _seed(db)
        bad = create_source(
            db, data_type="snapshot", name="坏源", impl_ref="tests.fake_sources:FakeFailCollector",
            role="主源", priority=1,
        )
        with pytest.raises(DataSourceError):
            call_source(db, bad)
        log = db.query(DataSourceLog).order_by(DataSourceLog.id.desc()).first()
        assert log.status == "fail"
        assert "模拟接口失效" in bad.last_error
    finally:
        db.close()


def test_crud_and_validation():
    db = SessionLocal()
    try:
        _seed(db)
        src = pick_sources(db, "kline")[0]
        update_source(db, src.id, priority=5, enabled=False)
        assert src.priority == 5 and src.enabled is False
        with pytest.raises(AppError):
            update_source(db, 99999, priority=1)
        delete_source(db, src.id)
        assert db.get(DataSourceConfig, src.id) is None
        with pytest.raises(AppError):
            create_source(db, data_type="kline", name="缺impl")  # 缺 impl_ref
    finally:
        db.close()


def test_test_connection_reports_result():
    db = SessionLocal()
    try:
        _seed(db)
        ok = pick_sources(db, "kline")[0]
        result = probe_source(db, ok.id)
        assert result["ok"] is True and result["count"] == 1

        bad = create_source(
            db, data_type="news", name="坏源2", impl_ref="tests.fake_sources:FakeFailCollector",
        )
        result2 = probe_source(db, bad.id)
        assert result2["ok"] is False and "模拟接口失效" in result2["error"]
    finally:
        db.close()


def test_rate_limiter_enforces_min_interval():
    sleeps = []
    t = [0.0]
    limiter = RateLimiter(clock=lambda: t[0], sleep=lambda s: (sleeps.append(s), t.__setitem__(0, t[0] + s)))
    limiter.wait("x", qps=2)   # 首次不等
    assert sleeps == []
    limiter.wait("x", qps=2)   # 间隔需 ≥0.5s
    assert sleeps and abs(sleeps[0] - 0.5) < 1e-6
