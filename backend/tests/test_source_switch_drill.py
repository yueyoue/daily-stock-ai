"""T1.11 切源演练（自动化验收）：停用 / 故障 / 全挂 / 恢复四场景走生产代码路径。

演练链路即生产链路：source_registry 选路 → collect_chain 降级 →
kline 同步入库 → data_source_log 切换留痕（M2 验收材料之一）。
实机（真实数据源）演练脚本见 backend/scripts/drill_switch_source.py（部署后执行）。
"""
from datetime import date

import pytest

from app.core.errors import AppError
from app.models.base import SessionLocal, init_db
from app.models.market import StockInfo, StockKline
from app.models.system import DataSourceConfig, DataSourceLog
from app.services.kline import sync_kline_for_code
from app.services.source_registry import create_source, pick_sources, update_source

from .fake_sources import FakeBackupKlineCollector, FakeKlineCollector


def setup_function():
    init_db()


def _clean(db):
    for model in (StockKline, StockInfo, DataSourceConfig, DataSourceLog):
        db.query(model).delete()
    db.commit()


def _setup(db):
    """登记演练主备两源（真实 impl_ref 动态加载机制，仅数据源为假实现）。"""
    FakeKlineCollector.reset()
    FakeBackupKlineCollector.reset()
    db.add(StockInfo(code="600000", name="演练股", list_date=date(2020, 1, 1)))
    db.commit()
    primary = create_source(db, data_type="kline", name="演练主源", role="主源",
                            impl_ref="tests.fake_sources:FakeKlineCollector", priority=1)
    backup = create_source(db, data_type="kline", name="演练备源", role="备用",
                           impl_ref="tests.fake_sources:FakeBackupKlineCollector", priority=2)
    return primary, backup


def _kline_rows(db, code="600000"):
    return (
        db.query(StockKline)
        .filter(StockKline.code == code)
        .order_by(StockKline.date.asc())
        .all()
    )


def _log_pairs(db):
    return [(l.source_name, l.status) for l in
            db.query(DataSourceLog).order_by(DataSourceLog.id.asc()).all()]


# ---------- 场景 1：停用主源 → 自动指向备源，数据完整 ----------

def test_drill_disable_primary_routes_to_backup():
    db = SessionLocal()
    try:
        _clean(db)
        primary, backup = _setup(db)

        # 演练动作：设置页停用 K 线主源
        update_source(db, primary.id, enabled=False)
        assert [s.name for s in pick_sources(db, "kline")] == ["演练备源"]

        stats = sync_kline_for_code(db, "600000", end_date=date(2020, 1, 10))
        rows = _kline_rows(db)
        assert stats["inserted"] == 10 and len(rows) == 10  # 数据完整
        assert rows[0].close == FakeBackupKlineCollector.base_price  # 取自备源
        assert FakeKlineCollector.calls == []               # 停用源零调用
        assert _log_pairs(db) == [("演练备源", "ok")]        # 切换过程无报错
    finally:
        db.close()


# ---------- 场景 2：主源故障 → 自动降级，data_source_log 留切换记录 ----------

def test_drill_primary_failure_fallback_with_switch_record():
    db = SessionLocal()
    try:
        _clean(db)
        primary, backup = _setup(db)
        FakeKlineCollector.fail = True  # 模拟主源接口失效（不禁用，走降级链）

        stats = sync_kline_for_code(db, "600000", end_date=date(2020, 1, 10))
        rows = _kline_rows(db)
        assert stats["inserted"] == 10 and len(rows) == 10  # 数据完整
        assert rows[0].close == FakeBackupKlineCollector.base_price
        # 切换痕迹：主源 fail → 备源 ok（支撑"数据源健康"页展示）
        assert _log_pairs(db) == [("演练主源", "fail"), ("演练备源", "ok")]
        assert backup.last_ok_at is not None and primary.last_error != ""
    finally:
        db.close()


# ---------- 场景 3：全挂 → 告警上抛，不写脏数据 ----------

def test_drill_all_sources_down_alerts_without_dirty_data():
    db = SessionLocal()
    try:
        _clean(db)
        _setup(db)
        FakeKlineCollector.fail = True
        FakeBackupKlineCollector.fail = True

        with pytest.raises(AppError) as ei:
            sync_kline_for_code(db, "600000", end_date=date(2020, 1, 10))
        assert ei.value.code == 502
        assert "演练主源" in ei.value.message and "演练备源" in ei.value.message
        assert _kline_rows(db) == []  # 全挂不落脏数据
        assert _log_pairs(db) == [("演练主源", "fail"), ("演练备源", "fail")]
    finally:
        db.close()


# ---------- 场景 4：恢复主源 → 选路按优先级回到主源 ----------

def test_drill_recover_primary_routes_back_by_priority():
    db = SessionLocal()
    try:
        _clean(db)
        primary, backup = _setup(db)

        # 停用期间：备源采上半段（01-01~01-05）
        update_source(db, primary.id, enabled=False)
        sync_kline_for_code(db, "600000", end_date=date(2020, 1, 5))
        assert FakeKlineCollector.calls == []
        assert len(_kline_rows(db)) == 5

        # 恢复主源：尾部增量（01-06~01-10）自动回到主源
        update_source(db, primary.id, enabled=True)
        stats = sync_kline_for_code(db, "600000", end_date=date(2020, 1, 10))
        rows = _kline_rows(db)
        assert stats["mode"] == "incremental"
        assert len(rows) == 10  # 数据完整（5 + 5）
        assert rows[-1].close == FakeKlineCollector.base_price + 9 * 0.1  # 尾段来自主源
        assert len(FakeBackupKlineCollector.calls) == 1  # 恢复后备源不再新增调用（仅停用期 1 次）
        assert _log_pairs(db) == [("演练备源", "ok"), ("演练主源", "ok")]
    finally:
        db.close()
