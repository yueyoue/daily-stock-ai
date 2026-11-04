"""回测数据口径（T5.5，开发文档 §4 模块5 口径四件套之 ①复权 ②幸存者偏差）。

- **复权**：K 线存不复权价 + 复权因子（T1.7 xdxr 推导的累计后复权因子）；
  回测统一按**后复权**计算：adj_price = price × adj_factor（展示转前复权在报告/前端层）
- **幸存者偏差**：股票池**含已退市标的**（默认 include_delisted=True，历史回测不得剔除）
- **数据质量**：复权因子缺失/非法（None/≤0）的标的**标记跳过并告警**（task_log），
  宁缺毋错——不带坏因子进回测

撮合规则（T+1/涨跌停/停牌/滑点）在 T5.6 引擎层，本层只供干净的价格序列。
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import distinct
from sqlalchemy.orm import Session

from app.models.market import StockInfo, StockKline
from app.services.pipeline import log_stage

logger = logging.getLogger("app.backtest_data")


class FactorMissingError(ValueError):
    """复权因子缺失/非法（调用方按'跳过该标的'处理并告警）。"""


def load_series(
    db: Session,
    code: str,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[dict]:
    """单标的日线序列（日期升序）：不复权 OHLCV + 后复权字段（×adj_factor）。

    任一交易日 adj_factor 缺失/非法 → 抛 FactorMissingError（整只跳过，避免口径污染）。
    """
    q = db.query(StockKline).filter(StockKline.code == code)
    if start_date is not None:
        q = q.filter(StockKline.date >= start_date)
    if end_date is not None:
        q = q.filter(StockKline.date <= end_date)
    rows = q.order_by(StockKline.date.asc()).all()
    out: list[dict] = []
    for r in rows:
        f = r.adj_factor
        if f is None or f <= 0:
            raise FactorMissingError(
                f"{code} {r.date} 复权因子缺失/非法（{f}），该标的跳过回测"
            )
        out.append({
            "date": r.date,
            "open": r.open, "high": r.high, "low": r.low, "close": r.close,
            "volume": r.volume, "amount": r.amount, "adj_factor": f,
            "adj_open": r.open * f, "adj_high": r.high * f,
            "adj_low": r.low * f, "adj_close": r.close * f,
        })
    return out


def iter_universe(db: Session, include_delisted: bool = True) -> list[str]:
    """回测股票池：有 K 线数据的全部标的（含退市，防幸存者偏差）。"""
    codes = [c for (c,) in db.query(distinct(StockKline.code)).order_by(StockKline.code).all()]
    if include_delisted:
        return codes
    delisted = {c for (c,) in db.query(StockInfo.code)
                .filter(StockInfo.is_delisted.is_(True)).all()}
    return [c for c in codes if c not in delisted]


def load_universe_series(
    db: Session,
    start_date: date | None = None,
    end_date: date | None = None,
    codes: list[str] | None = None,
    include_delisted: bool = True,
) -> tuple[dict[str, list[dict]], list[str]]:
    """批量加载股票池序列：返回 ({code: 序列}, 跳过清单)。

    因子缺失的标的跳过并写 task_log 告警（"回测数据:跳过标的"，T4.6 可见）。
    """
    targets = codes if codes is not None else iter_universe(db, include_delisted)
    data: dict[str, list[dict]] = {}
    skipped: list[str] = []
    for code in targets:
        try:
            series = load_series(db, code, start_date, end_date)
        except FactorMissingError as e:
            skipped.append(code)
            logger.warning("回测数据跳过 %s：%s", code, e)
            log_stage(db, "回测数据:跳过标的", "warning", message=str(e))
            continue
        if series:
            data[code] = series
    return data, skipped
