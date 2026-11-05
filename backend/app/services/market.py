"""大盘汇总入库服务（任务 T1.9）。

- upsert_market_daily：按日期合并写入 market_daily——各采集线只是**局部视图**，
  非空字段覆盖、extra 合并（资金/涨跌停/家数三条线合成一行）
- sync_market_daily：三条采集线（moneyflow / limit_updown / market_summary）
  各走采集链（主备降级），单线失败不影响其他线（errors 汇总返回）；
  全部失败才抛 502

⚠️ 缺陷 D-01 修复：数值列与 extra 统一走 safe_float/json_safe，NaN / ±Inf 不入库
（MySQL JSON 列不接受 NaN，会报 3140 并连锁卡死任务行）；commit 失败先 rollback 再抛。
"""
import logging
from datetime import date

from sqlalchemy.orm import Session

from app.collectors.util import json_safe, safe_float
from app.core.errors import AppError
from app.models.market import MarketDaily
from app.services.collect_chain import collect_with_fallback

logger = logging.getLogger("app.market")

_FIELDS = ("up_count", "down_count", "limit_up_count", "limit_down_count",
           "sentiment_score", "main_moneyflow")
DEFAULT_DATA_TYPES = ("moneyflow", "limit_updown", "market_summary")


def upsert_market_daily(db: Session, records: list[dict]) -> dict:
    """合并写入 market_daily（记录间同日期互相覆盖非空字段）。

    批内同日期用实例缓存合并，防止未 flush 前重复建行撞主键。
    """
    cache: dict[date, MarketDaily] = {}
    inserted = updated = 0
    for rec in records:
        d = rec.get("date")
        if isinstance(d, str):
            d = date.fromisoformat(d[:10])
        if d is None:
            continue
        row = cache.get(d)
        if row is None:
            row = db.get(MarketDaily, d)
            if row is None:
                row = MarketDaily(date=d)
                db.add(row)
                inserted += 1
            else:
                updated += 1
            cache[d] = row

        for field in _FIELDS:
            v = safe_float(rec.get(field))  # NaN / ±Inf / 空 → None，不写库（D-01）
            if v is not None:
                setattr(row, field, v)
        if rec.get("extra"):
            merged = dict(row.extra or {})
            merged.update(json_safe(rec["extra"]))  # NaN / ±Inf → None，JSON 列方可入库（D-01）
            row.extra = merged
    try:
        db.commit()
    except Exception:
        # 会话可能因 flush 失败进入 pending-rollback；先回滚再抛，避免掩盖真实错误（D-01）
        db.rollback()
        raise
    return {"dates": len(cache), "inserted": inserted, "updated": updated}


def sync_market_daily(
    db: Session,
    day: date | None = None,
    data_types: tuple = DEFAULT_DATA_TYPES,
) -> dict:
    """当日市场汇总同步：三条采集线合成一行，返回统计与分线错误。"""
    day = day or date.today()
    merged: dict[date, dict] = {}
    errors: list[str] = []
    for data_type in data_types:
        try:
            rows = collect_with_fallback(db, data_type, date=day.isoformat())
        except AppError as e:
            logger.warning("[%s] 采集线失败：%s", data_type, e.message)
            errors.append(f"{data_type}: {e.message}")
            continue
        for row in rows:
            d = row.get("date")
            if isinstance(d, str):
                d = date.fromisoformat(d[:10])
            if d is None:
                continue
            slot = merged.setdefault(d, {"date": d})
            for k, v in row.items():
                if k == "extra":
                    slot.setdefault("extra", {}).update(v or {})
                elif v is not None:
                    slot[k] = v

    if not merged:
        raise AppError(502, "market_daily 全部采集线失败: " + "; ".join(errors))
    stats = upsert_market_daily(db, list(merged.values()))
    stats["errors"] = errors
    if errors:
        stats["complete"] = False
    else:
        stats["complete"] = True  # 验收口径：当日 market_daily 字段齐全
    logger.info("[%s] market_daily 同步 %s", day, stats)
    return stats