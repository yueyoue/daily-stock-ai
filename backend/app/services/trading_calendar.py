"""交易日历服务（开发文档 §4.9）：调度前置判断"是否交易日"。

- 数据来源：data_source_config 登记的交易日历接口（初始 akshare），由采集线填充 trade_calendar 表
- 兜底：日历缺失时按"周一~周五视为交易日"判断（法定节假日需采集线及时补日历）
"""
import logging
from datetime import date

from sqlalchemy.orm import Session

from app.models.system import TradeCalendar

logger = logging.getLogger("app.calendar")


def is_trading_day(db: Session, day: date) -> bool:
    row = db.get(TradeCalendar, day)
    if row is not None:
        return row.is_trading_day
    logger.warning("交易日历缺失 %s，按周一~周五兜底判断", day.isoformat())
    return day.weekday() < 5


def previous_trading_day(db: Session, day: date, max_back: int = 20) -> date:
    """上一个交易日（用于隔夜资讯/大盘/资金取数的基准日）。"""
    from datetime import timedelta

    cur = day - timedelta(days=1)
    for _ in range(max_back):
        if is_trading_day(db, cur):
            return cur
        cur = cur - timedelta(days=1)
    logger.warning("回溯 %d 天未找到交易日（基准 %s），按前一日返回", max_back, day.isoformat())
    return day - timedelta(days=1)


def upsert_calendar(db: Session, entries: list[dict]) -> int:
    """批量写入日历。entries: [{"date": date|str, "is_trading_day": bool}, ...]"""
    count = 0
    for e in entries:
        d = e["date"]
        if isinstance(d, str):
            d = date.fromisoformat(d)
        row = db.get(TradeCalendar, d)
        if row is None:
            row = TradeCalendar(date=d, is_trading_day=bool(e["is_trading_day"]))
            db.add(row)
        else:
            row.is_trading_day = bool(e["is_trading_day"])
        count += 1
    db.commit()
    return count
