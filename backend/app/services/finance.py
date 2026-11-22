"""财务数据入库服务（任务 T1.8）。

- upsert_finance：按 (code, report_date) upsert；空值不覆盖已有科目（轻量源保护）
- sync_finance_for_code：采集链取数（主备降级）→ 入库。财务数据量小（每股约
  40~80 个报告期），季度增量 = 全量拉取 + 幂等 upsert，无需断点续采
- sync_finance_limited / prune_finance：10 年滚动窗口（2026-11-22 追加）
  接口无时间参数，只能全量拉取后按报告期截断入库；入库后再按票剪枝超窗行
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


# ---------------- 10 年滚动窗口（零侵入包装，2026-11-22） ----------------
DEFAULT_MAX_PERIODS = 40  # 40 个报告期 ≈ 10 年（季报口径；比"日期减 10 年"更稳，避开披露滞后）


def _norm_date(value):
    """报告期归一：字符串 ISO / date 皆可，非法返回 None。"""
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except Exception:  # noqa: BLE001
            return None
    return value


def sync_finance_limited(db: Session, code: str, max_periods: int = DEFAULT_MAX_PERIODS,
                         data_type: str = "finance") -> dict:
    """只入库最近 max_periods 期。

    东财三表接口不给时间参数（整段返回，000001 实测 123 期≈30 年），
    所以策略是「全量拉下来、只留最近 N 期入库」，下载量不变、库体积可控。
    """
    records = collect_with_fallback(db, data_type, code=code)
    dropped = 0
    if max_periods and max_periods > 0 and records:
        kept: list[dict] = []
        for rec in records:
            d = _norm_date(rec.get("report_date"))
            if d is None:
                continue
            rec["report_date"] = d
            kept.append(rec)
        kept.sort(key=lambda r: r["report_date"], reverse=True)
        dropped = max(len(kept) - max_periods, 0)
        records = kept[:max_periods]
    stats = upsert_finance(db, records)
    stats["dropped_by_window"] = dropped
    logger.info("[%s] 财务入库(窗口 %s 期, 丢弃 %d) %s", code, max_periods, dropped, stats)
    return stats


def prune_finance(db: Session, code: str, max_periods: int = DEFAULT_MAX_PERIODS,
                  dry_run: bool = False, archive_dir: str | None = None) -> dict:
    """按票滚动保留最近 max_periods 期，删除更早的报表行与指标行。

    安全约束：
    - 只处理传入的单个 code，绝不跨票批量删；
    - dry_run=True 只报数不动数据；
    - archive_dir 给定时，删除前把待删行导出 JSON（可人工恢复）。
    """
    import json
    import os

    from sqlalchemy import desc

    from app.models.market import StockFinIndicator

    if not max_periods or max_periods <= 0:
        return {"code": code, "finance_deleted": 0, "indicator_deleted": 0, "kept": None}

    dates = [d for (d,) in db.query(StockFinance.report_date)
             .filter(StockFinance.code == code)
             .order_by(desc(StockFinance.report_date)).all()]
    if len(dates) <= max_periods:
        return {"code": code, "finance_deleted": 0, "indicator_deleted": 0, "kept": len(dates)}

    kept, drop = dates[:max_periods], dates[max_periods:]
    info = {"code": code, "finance_deleted": len(drop), "kept": len(kept),
            "drop_range": [str(drop[-1]), str(drop[0])]}
    if dry_run:
        info["indicator_deleted"] = None
        info["dry_run"] = True
        return info

    if archive_dir:
        try:
            os.makedirs(archive_dir, exist_ok=True)
            rows = (db.query(StockFinance)
                    .filter(StockFinance.code == code, StockFinance.report_date.in_(drop)).all())
            payload = [{"code": r.code, "report_date": str(r.report_date),
                        "report_type": r.report_type, "total_revenue": r.total_revenue,
                        "net_profit": r.net_profit, "operating_cashflow": r.operating_cashflow,
                        "total_assets": r.total_assets, "total_liabilities": r.total_liabilities,
                        "equity": r.equity, "eps": r.eps, "extra": r.extra or {}} for r in rows]
            path = os.path.join(archive_dir, f"{code}_{date.today():%Y%m%d}_prune.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] 剪枝归档失败（继续删除）：%s", code, e)

    fdel = (db.query(StockFinance)
            .filter(StockFinance.code == code, StockFinance.report_date.in_(drop))
            .delete(synchronize_session=False))
    idel = (db.query(StockFinIndicator)
            .filter(StockFinIndicator.code == code, StockFinIndicator.report_date.in_(drop))
            .delete(synchronize_session=False))
    db.commit()
    info["finance_deleted"] = fdel
    info["indicator_deleted"] = idel
    logger.info("[%s] 剪枝：报表 -%d 指标 -%d（保留 %d 期）", code, fdel, idel, len(kept))
    return info


def set_announce_dates(db: Session, code: str, pairs: list[tuple]) -> int:
    """回填公告日期（来源：业绩报表 NOTICE_DATE）。pairs = [(报告期, 公告日), ...]"""
    from sqlalchemy import update

    n = 0
    for rd, ad in pairs:
        rd, ad = _norm_date(rd), _norm_date(ad)
        if rd is None or ad is None:
            continue
        res = db.execute(update(StockFinance)
                         .where(StockFinance.code == code, StockFinance.report_date == rd)
                         .values(announce_date=ad))
        n += res.rowcount or 0
    db.commit()
    return n