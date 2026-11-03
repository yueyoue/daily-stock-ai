"""K 线入库与断点续采服务（任务 T1.7）。

- upsert_kline：不复权 OHLCV + 复权因子入库（(code,date) 联合主键 upsert）
- plan_ranges：按已采范围规划待采区间（断点续采核心）：
  * 无数据 → 全量 [上市日/20 年前, 今天]
  * 有数据 → 头部缺口补采 + 尾部增量（断点重跑自动续上）
- sync_kline_for_code：规划 → 采集链取数（主备降级）→ 入库 → 更新续采记录
"""
import logging
from datetime import date, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.market import StockInfo, StockKline
from app.models.system import CollectProgress
from app.services.collect_chain import collect_with_fallback

logger = logging.getLogger("app.kline")

LOOKBACK_YEARS = 20  # §5.2：20 年上限（不足按实际上市日）


def upsert_kline(db: Session, records: list[dict]) -> dict:
    """入库标准化 K 线记录（同 (code,date) 先到先得；重复执行幂等）。"""
    inserted = updated = 0
    seen: set[tuple[str, date]] = set()
    for rec in records:
        code = str(rec.get("code", "")).strip()
        d = rec.get("date")
        if isinstance(d, str):
            d = date.fromisoformat(d[:10])
        if not code or d is None or (code, d) in seen:
            continue
        seen.add((code, d))
        fields = dict(
            open=float(rec["open"]), high=float(rec["high"]), low=float(rec["low"]),
            close=float(rec["close"]),
            volume=float(rec.get("volume") or 0), amount=float(rec.get("amount") or 0),
            adj_factor=float(rec.get("adj_factor") or 1.0),
        )
        row = db.get(StockKline, (code, d))
        if row is None:
            db.add(StockKline(code=code, date=d, **fields))
            inserted += 1
        else:
            for k, v in fields.items():
                setattr(row, k, v)
            updated += 1
    db.commit()
    return {"received": len(seen), "inserted": inserted, "updated": updated}


def plan_ranges(
    db: Session,
    code: str,
    end_date: date | None = None,
    lookback_years: int = LOOKBACK_YEARS,
    force_full: bool = False,
) -> dict:
    """规划待采区间（断点续采）。返回 {code, mode, ranges:[{start,end}]}。

    mode: full=首次全量 / resume=有头部缺口补采 / incremental=仅尾部增量 / up_to_date=无需采
    头部缺口判定：仅当续采记录为 partial（上轮采集中断）时补采头部；
    complete 记录信任现有起点即上市日（避免每轮向 20 年前空跑回填）。
    20 年下限受上市日钳制（stock_info.list_date）。
    """
    end_date = end_date or date.today()
    floor = end_date - timedelta(days=365 * lookback_years + 1)
    info = db.get(StockInfo, code)
    if info is not None and info.list_date is not None and info.list_date > floor:
        floor = info.list_date

    min_d, max_d = (
        db.query(func.min(StockKline.date), func.max(StockKline.date))
        .filter(StockKline.code == code)
        .one()
    )
    if force_full or min_d is None or max_d is None:
        return {"code": code, "mode": "full", "ranges": [{"start": floor, "end": end_date}]}

    progress = (
        db.query(CollectProgress)
        .filter(CollectProgress.code == code, CollectProgress.data_type == "kline")
        .first()
    )
    head_trusted = progress is not None and progress.status == "complete"

    ranges: list[dict] = []
    if not head_trusted and min_d > floor:
        ranges.append({"start": floor, "end": min_d - timedelta(days=1)})
    if max_d < end_date:
        ranges.append({"start": max_d + timedelta(days=1), "end": end_date})
    if not ranges:
        mode = "up_to_date"
    elif ranges[0]["start"] == floor:
        mode = "resume"
    else:
        mode = "incremental"
    return {"code": code, "mode": mode, "ranges": ranges}


def update_progress(
    db: Session,
    code: str,
    first_date: date | None,
    last_date: date | None,
    data_type: str = "kline",
    status: str = "partial",
    note: str = "",
) -> None:
    """记录已采范围（断点续采记录表，(code,data_type) 唯一）。"""
    row = (
        db.query(CollectProgress)
        .filter(CollectProgress.code == code, CollectProgress.data_type == data_type)
        .first()
    )
    if row is None:
        row = CollectProgress(code=code, data_type=data_type)
        db.add(row)
    row.first_date = first_date
    row.last_date = last_date
    row.status = status
    if note:
        row.note = note[:256]
    db.commit()


def sync_kline_for_code(
    db: Session,
    code: str,
    end_date: date | None = None,
    data_type: str = "kline",
    force_full: bool = False,
) -> dict:
    """单标的 K 线同步：规划区间 → 采集（主备降级）→ 入库 → 更新续采记录。

    中途失败时已完成区间照常入库并记录进度（重跑自动续采剩余区间）。
    """
    plan = plan_ranges(db, code, end_date=end_date, force_full=force_full)
    stats = {"code": code, "mode": plan["mode"], "ranges": len(plan["ranges"]),
             "received": 0, "inserted": 0, "updated": 0}
    if not plan["ranges"]:
        logger.info("[%s] K线已最新，无需采集", code)
        return stats

    for i, r in enumerate(plan["ranges"]):
        records = collect_with_fallback(
            db, data_type,
            code=code, start_date=r["start"].isoformat(), end_date=r["end"].isoformat(),
        )
        s = upsert_kline(db, records)
        for k in ("received", "inserted", "updated"):
            stats[k] += s[k]
        done = i == len(plan["ranges"]) - 1
        min_d, max_d = (
            db.query(func.min(StockKline.date), func.max(StockKline.date))
            .filter(StockKline.code == code)
            .one()
        )
        update_progress(db, code, min_d, max_d, data_type=data_type,
                        status="complete" if done else "partial")
        logger.info("[%s] %s~%s 入库 %d 条（%s）", code, r["start"], r["end"], s["received"], plan["mode"])
    return stats
