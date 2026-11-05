"""资讯入库服务（T6.1）：去重入库（dedup_hash 唯一），增量安全。

- 同一条不重复入库：入库前按 dedup_hash 预查 + 唯一约束兜底（并发竞态容错）
- impact_level 留空给 T6.2 LLM 分级；分级结果回填不走本服务
"""
import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.content import News

logger = logging.getLogger("app.news")


def ingest_news(db: Session, records: list[dict]) -> dict:
    """去重入库标准化资讯记录，返回统计 {inserted, duplicates, skipped}。"""
    inserted = duplicates = skipped = 0
    for rec in records:
        dedup = rec.get("dedup_hash")
        title = rec.get("title")
        if not dedup or not title:
            skipped += 1
            continue
        exists = db.query(News.id).filter(News.dedup_hash == dedup).first()
        if exists is not None:
            duplicates += 1
            continue
        db.add(News(**rec))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()  # 并发/竞态下唯一约束兜底
            duplicates += 1
            continue
        inserted += 1
    logger.info("资讯入库：新增 %d，重复 %d，跳过 %d", inserted, duplicates, skipped)
    return {"inserted": inserted, "duplicates": duplicates, "skipped": skipped}
