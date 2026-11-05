"""T6.1 验收（CI 可测部分）：标题/时间归一、去重哈希、列名映射、采集流程、去重入库。

真机验收：资讯入库带来源/时间（akshare 实测联调）；限流 QPS≤0.5 由接口配置约束。
"""
from datetime import datetime

from app.collectors.news import (
    ClsNewsCollector,
    dedup_hash,
    normalize_news,
    normalize_title,
    parse_datetime,
)
from app.models.base import SessionLocal, init_db
from app.models.content import News
from app.services.news import ingest_news


def _clean(db):
    db.query(News).delete()
    db.commit()


def test_normalize_title():
    assert normalize_title("  隔夜 外围  ") == "隔夜 外围"
    assert normalize_title("全\u3000角空格") == "全 角空格"
    assert normalize_title("多\n行\n文本") == "多 行 文本"


def test_parse_datetime_formats():
    assert parse_datetime("2026-11-05 08:30:00") == datetime(2026, 11, 5, 8, 30)
    assert parse_datetime("2026-11-05 08:30") == datetime(2026, 11, 5, 8, 30)
    assert parse_datetime("2026/11/05 08:30:00") == datetime(2026, 11, 5, 8, 30)
    assert parse_datetime(None) is None
    assert parse_datetime("不是时间") is None


def test_dedup_hash_stable_and_sensitive():
    t = datetime(2026, 11, 5, 8, 30, 45)
    h1 = dedup_hash("隔夜外围大涨", t)
    h2 = dedup_hash("  隔夜外围大涨 ", datetime(2026, 11, 5, 8, 30, 59))  # 秒差异不影响
    h3 = dedup_hash("隔夜外围大涨", datetime(2026, 11, 5, 8, 31))  # 分钟不同 → 不同键
    h4 = dedup_hash("隔夜外围小涨", t)
    assert h1 == h2          # 标题归一 + 分钟级归一
    assert h1 != h3 and h1 != h4
    assert len(h1) == 64


def test_normalize_news_maps_chinese_columns():
    raw = {"标题": " 国内算力政策落地 ", "内容": "正文…", "发布时间": "2026-11-05 07:21",
           "链接": "https://example.com/1"}
    rec = normalize_news(raw, source="财联社电报")
    assert rec is not None
    assert rec["title"] == "国内算力政策落地"
    assert rec["source"] == "财联社电报"
    assert rec["published_at"] == datetime(2026, 11, 5, 7, 21)
    assert rec["url"] == "https://example.com/1"
    assert rec["impact_level"] == ""            # 留给 T6.2 分级
    assert len(rec["dedup_hash"]) == 64


def test_normalize_news_skips_empty_title():
    assert normalize_news({"标题": "   "}, source="x") is None
    assert normalize_news({}, source="x") is None


def test_collector_fetch_flow(monkeypatch):
    collector = ClsNewsCollector()
    monkeypatch.setattr(collector, "_fetch_raw", lambda **params: [
        {"标题": "消息A", "发布时间": "2026-11-05 08:00"},
        {"标题": "", "发布时间": "2026-11-05 08:01"},  # 无标题跳过
    ])
    records = collector.fetch()
    assert len(records) == 1
    assert records[0]["source"] == "财联社电报"


def test_ingest_news_dedup():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        rec = normalize_news({"标题": "消息A", "发布时间": "2026-11-05 08:00"}, source="财联社电报")
        dup = normalize_news({"标题": " 消息A ", "发布时间": "2026-11-05 08:00:30"}, source="同花顺快讯")

        s1 = ingest_news(db, [rec])
        assert s1 == {"inserted": 1, "duplicates": 0, "skipped": 0}

        s2 = ingest_news(db, [dup])   # 归一后同一条 → 不重复入库
        assert s2 == {"inserted": 0, "duplicates": 1, "skipped": 0}
        assert db.query(News).count() == 1

        s3 = ingest_news(db, [{"title": "", "dedup_hash": ""}])  # 缺键跳过
        assert s3["skipped"] == 1
    finally:
        _clean(db)
        db.close()
