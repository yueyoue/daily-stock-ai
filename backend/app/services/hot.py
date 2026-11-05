"""热点题材归档服务（T6.3）：按 (日期, 标题) 幂等归档。

- 同日同名概念再次归档 → 刷新热度/关联个股（不重复建行）
- 归档与检索解耦：检索 API 已由 T3.4 提供（板块/关键词/日期过滤）
"""
import logging
from datetime import date

from sqlalchemy.orm import Session

from app.models.content import HotTopic

logger = logging.getLogger("app.hot")


def archive_hot_topics(db: Session, records: list[dict]) -> dict:
    """归档热点记录，返回统计 {inserted, updated, skipped}。"""
    inserted = updated = skipped = 0
    for rec in records:
        title = str(rec.get("title") or "").strip()
        day = rec.get("date") or date.today()
        if not title:
            skipped += 1
            continue
        row = (
            db.query(HotTopic)
            .filter(HotTopic.date == day, HotTopic.title == title)
            .first()
        )
        if row is None:
            db.add(HotTopic(
                date=day, title=title,
                heat=str(rec.get("heat") or "中"),
                industry_chain=str(rec.get("industry_chain") or "")[:256],
                related_codes=list(rec.get("related_codes") or []),
            ))
            inserted += 1
        else:
            row.heat = str(rec.get("heat") or row.heat)
            row.industry_chain = str(rec.get("industry_chain") or row.industry_chain)[:256]
            row.related_codes = list(rec.get("related_codes") or row.related_codes)
            updated += 1
    db.commit()
    logger.info("热点归档：新增 %d，更新 %d，跳过 %d", inserted, updated, skipped)
    return {"inserted": inserted, "updated": updated, "skipped": skipped}
