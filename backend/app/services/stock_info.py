"""股票基础信息入库服务（任务 T1.6）。

- upsert_stock_info：全市场基础信息 upsert（新增/更新/去重），返回入库统计
- 退市标记（防幸存者偏差，§4 模块5）：全量刷新时，库内存在但本次未出现的标的
  标记 is_delisted=True；标的重新出现自动恢复
- 空值保护：备源/轻量源采集不到的字段（行业/概念）**不覆盖**已有标签
- 安全护栏：全量刷新的 delisted 标记要求本次记录数 ≥ min_full_rows，
  防止截断/降级采样误标大片退市
"""
import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.market import StockInfo

logger = logging.getLogger("app.stock_info")

# 全市场量级护栏：低于该行数不执行"缺失即退市"标记（全 A 约 5300 只）
MIN_FULL_ROWS = 3000


def upsert_stock_info(
    db: Session,
    records: list[dict],
    full_refresh: bool = True,
    min_full_rows: int = MIN_FULL_ROWS,
) -> dict:
    """入库标准化股票基础信息记录，返回统计（含标签非空率，验收口径用）。

    - full_refresh=True：本次为全市场快照，未出现的库内标的标记退市（有量级护栏）
    - full_refresh=False：增量/局部更新，不动退市标记
    """
    inserted = updated = 0
    seen: set[str] = set()
    for rec in records:
        code = str(rec.get("code", "")).strip()
        if not code:
            continue
        seen.add(code)
        name = str(rec.get("name", "")).strip()
        industry = str(rec.get("industry", "")).strip()
        concepts = list(rec.get("concepts") or [])
        market = str(rec.get("market", "")).strip()

        row = db.get(StockInfo, code)
        if row is None:
            db.add(
                StockInfo(
                    code=code,
                    name=name,
                    industry=industry,
                    concepts=concepts,
                    market=market,
                    is_delisted=False,
                )
            )
            inserted += 1
            continue
        # 空值保护：非空才覆盖（轻量源不带标签时不抹掉已有数据）
        if name:
            row.name = name
        if industry:
            row.industry = industry
        if concepts:
            row.concepts = concepts
        if market:
            row.market = market
        row.is_delisted = False  # 重新出现在全市场列表 → 恢复在市
        updated += 1

    delisted_marked = 0
    delisted_skipped = False
    if full_refresh and seen:
        if len(seen) >= min_full_rows:
            for row in db.query(StockInfo).filter(StockInfo.code.notin_(seen)).all():
                if not row.is_delisted:
                    row.is_delisted = True
                    delisted_marked += 1
            if delisted_marked:
                logger.info("全量刷新：未出现标的标记退市 %d 只", delisted_marked)
        else:
            # 护栏：样本量不足视为非全市场快照，跳过退市标记
            delisted_skipped = True
            logger.warning(
                "全量刷新样本 %d 条 < 护栏 %d，跳过退市标记（疑似截断/采样）",
                len(seen),
                min_full_rows,
            )

    db.commit()

    total = db.query(func.count(StockInfo.code)).scalar() or 0
    industry_filled = (
        db.query(func.count(StockInfo.code))
        .filter(StockInfo.industry != "")
        .scalar()
        or 0
    )
    concept_filled = (
        db.query(func.count(StockInfo.code))
        .filter(func.json_length(StockInfo.concepts) > 0)
        .scalar()
        or 0
    )
    return {
        "received": len(seen),
        "inserted": inserted,
        "updated": updated,
        "delisted_marked": delisted_marked,
        "delisted_skipped": delisted_skipped,
        "total": total,
        "industry_filled": industry_filled,
        "concept_filled": concept_filled,
        # 验收口径：行业/概念标签非空率 >95%（T1.6）
        "industry_rate": round(industry_filled / total, 4) if total else 0.0,
        "concept_rate": round(concept_filled / total, 4) if total else 0.0,
    }
