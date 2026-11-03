"""大盘仪表盘数据服务（T3.2，开发文档 §3.2）。

- 四卡片：情绪得分 / 涨停家数 / 主力资金（东财口径，§10.7 不用北向） / 涨跌家数
- 情绪趋势曲线（近 N 交易日）与主线板块资金条
- 数据源：market_daily（T1.9 三线合成）；主线板块取 extra.main_sectors，
  缺省回落 hot_topic 当日热点（资金字段为空，待 T6.3 板块资金采集接入）
"""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from app.models.content import HotTopic
from app.models.market import MarketDaily


def _sectors_for(db: Session, d: date | None, extra: dict) -> list[dict]:
    raw = (extra or {}).get("main_sectors")
    if isinstance(raw, list) and raw:
        return [{"name": str(s.get("name", "")), "moneyflow": s.get("moneyflow")}
                for s in raw[:10]]
    if d is None:
        return []
    rows = (
        db.query(HotTopic)
        .filter(HotTopic.date == d)
        .order_by(HotTopic.id.desc())
        .limit(6)
        .all()
    )
    return [{"name": r.title, "moneyflow": None, "heat": r.heat} for r in rows]


def get_market_dashboard(db: Session, days: int = 20) -> dict:
    """仪表盘聚合（days=情绪趋势窗口，5~120）。"""
    latest = db.query(MarketDaily).order_by(MarketDaily.date.desc()).first()
    trend_rows = (
        db.query(MarketDaily)
        .order_by(MarketDaily.date.desc())
        .limit(max(5, min(days, 120)))
        .all()
    )[::-1]
    trend = [
        {"date": r.date.isoformat(), "sentiment_score": r.sentiment_score}
        for r in trend_rows
    ]
    if latest is None:
        return {
            "trade_date": None, "sentiment_score": None,
            "limit_up_count": None, "limit_down_count": None,
            "max_consecutive": None, "consecutive_ladder": {},
            "main_moneyflow": None,
            "up_count": None, "down_count": None,
            "sentiment_trend": trend, "main_sectors": [],
        }
    extra = latest.extra or {}
    return {
        "trade_date": latest.date.isoformat(),
        "sentiment_score": latest.sentiment_score,
        "limit_up_count": latest.limit_up_count,
        "limit_down_count": latest.limit_down_count,
        "max_consecutive": extra.get("最高连板"),
        "consecutive_ladder": extra.get("连板梯队") or {},
        "main_moneyflow": latest.main_moneyflow,
        "up_count": latest.up_count,
        "down_count": latest.down_count,
        "sentiment_trend": trend,
        "main_sectors": _sectors_for(db, latest.date, extra),
    }
