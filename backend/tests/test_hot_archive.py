"""T6.3 验收（CI 可测部分）：热度分级、概念标准化、TopN 截断、幂等归档。

真机验收：热点卡片含产业链+关联个股、检索生效（T3.4 API 已覆盖检索）。
"""
from datetime import date

from app.collectors.hot import AkshareConceptCollector, grade_heat, normalize_concept
from app.models.base import SessionLocal, init_db
from app.models.content import HotTopic
from app.services.hot import archive_hot_topics

D = date(2026, 11, 5)


def _clean(db):
    db.query(HotTopic).delete()
    db.commit()


def test_grade_heat_boundaries():
    assert grade_heat(3.0) == "高"
    assert grade_heat(5.2) == "高"
    assert grade_heat(0.0) == "中"
    assert grade_heat(2.9) == "中"
    assert grade_heat(-1.5) == "低"
    assert grade_heat(None) == "中"


def test_normalize_concept_maps_columns():
    raw = {"概念名": "算力基础设施", "涨跌幅": "4.12"}
    rec = normalize_concept(raw, date_=D, members=["600000", "000001", "", "300750"])
    assert rec["title"] == "算力基础设施"
    assert rec["heat"] == "高"
    assert rec["industry_chain"] == "算力基础设施"
    assert rec["related_codes"] == ["600000", "000001", "300750"]
    assert rec["date"] == D


def test_normalize_concept_truncates_codes():
    rec = normalize_concept({"概念名": "X"}, date_=D, members=[str(i) for i in range(100)])
    assert len(rec["related_codes"]) == 30  # MAX_CODES


def test_collector_topn_by_heat(monkeypatch):
    collector = AkshareConceptCollector()
    rows = [
        {"概念名": "冷门", "涨跌幅": "0.2"},
        {"概念名": "热门", "涨跌幅": "9.9"},
        {"概念名": "退潮", "涨跌幅": "-8.0"},
    ]
    members = {"热门": ["600000"]}
    monkeypatch.setattr(collector, "_fetch_raw", lambda **params: (rows, members))

    records = collector.fetch(top_n=2, date=D)
    assert [r["title"] for r in records] == ["热门", "退潮"]  # |涨跌幅| Top2
    assert records[0]["related_codes"] == ["600000"]
    assert records[1]["heat"] == "低"


def test_archive_hot_topics_idempotent():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        rec = normalize_concept({"概念名": "算力", "涨跌幅": "4"}, date_=D, members=["600000"])
        s1 = archive_hot_topics(db, [rec])
        assert s1 == {"inserted": 1, "updated": 0, "skipped": 0}

        rec2 = normalize_concept({"概念名": "算力", "涨跌幅": "-2"}, date_=D, members=["600000", "000001"])
        s2 = archive_hot_topics(db, [rec2])  # 同日同名 → 更新
        assert s2 == {"inserted": 0, "updated": 1, "skipped": 0}
        assert db.query(HotTopic).count() == 1

        row = db.query(HotTopic).first()
        assert row.heat == "低"                       # 热度刷新
        assert row.related_codes == ["600000", "000001"]  # 关联个股刷新

        s3 = archive_hot_topics(db, [{"date": D, "title": ""}])
        assert s3["skipped"] == 1
    finally:
        _clean(db)
        db.close()
