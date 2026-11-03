"""财务数据入库服务（任务 T1.8）。

- upsert_finance：按 (code, report_date) upsert；空值不覆盖已有科目（轻量源保护）
- sync_finance_for_code：采集链取数（主备降级）→ 入库。财务数据量小（每股约
  40~80 个报告期），季度增量 = 全量拉取 + 幂等 upsert，无需断点续采
"""
import logging
from datetime import date

from sqlalchemy.orm import Session

from app.models.market import StockFinance
from app.services.collect_chain import collect_with_fallback

logger = logging.getLogger("app.finance")

_NUMERIC_FIELDS = (
    "total_revenue", "net_profit", "operating_cashflow",
    "total_assets", "total_liabilities", "equity", "eps",
)


def upsert_finance(db: Session, records: list[dict]) -> dict:
    """入库标准化财务记录（同 (code, report_date) 先到先得；重复执行幂等）。"""
    inserted = updated = 0
    seen: set[tuple[str, date]] = set()
    for rec in records:
        code = str(rec.get("code", "")).strip()
        d = rec.get("report_date")
        if isinstance(d, str):
            d = date.fromisoformat(d[:10])
        if not code or d is None or (code, d) in seen:
            continue
        seen.add((code, d))

        row = db.get(StockFinance, (code, d))
        if row is None:
            row = StockFinance(code=code, report_date=d,
                               report_type=str(rec.get("report_type") or "quarterly"))
            db.add(row)
            inserted += 1
        else:
            updated += 1
        for field in _NUMERIC_FIELDS:
            v = rec.get(field)
            if v is not None:  # 空值不覆盖（备源科目不全时保护已有数据）
                setattr(row, field, float(v))
        if rec.get("extra"):
            merged = dict(row.extra or {})
            merged.update(rec["extra"])
            row.extra = merged
    db.commit()
    return {"received": len(seen), "inserted": inserted, "updated": updated}


def sync_finance_for_code(db: Session, code: str, data_type: str = "finance") -> dict:
    """单标的财务同步：全量拉取（主备降级）→ 幂等入库，返回统计。"""
    records = collect_with_fallback(db, data_type, code=code)
    stats = upsert_finance(db, records)
    logger.info("[%s] 财务入库 %s", code, stats)
    return stats
