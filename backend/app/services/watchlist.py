"""观察池快照预览服务（T6.5，开发文档 §4模块7）。

- 行情：stock_kline 最新两个交易日 → 最新收盘 / 涨跌幅（%）
- ROE：stock_fin_indicator 最新报告期
- 概念/行业：stock_info 标签
- 市值：暂无入库数据列（待快照线扩展），返回 None（UI 显示 —）
"""
from sqlalchemy.orm import Session

from app.models.market import StockFinIndicator, StockInfo, StockKline


def preview_for_codes(db: Session, codes: list[str]) -> dict[str, dict]:
    """按代码批量取预览快照，返回 {code: preview_dict}。"""
    out: dict[str, dict] = {
        c: {
            "latest_date": None,
            "close": None,
            "pct_change": None,
            "roe": None,
            "industry": "",
            "concepts": [],
            "market_cap": None,
        }
        for c in codes
    }
    if not codes:
        return out

    for row in db.query(StockInfo).filter(StockInfo.code.in_(codes)).all():
        out[row.code]["industry"] = row.industry or ""
        out[row.code]["concepts"] = list(row.concepts or [])[:6]

    for code in codes:
        rows = (
            db.query(StockKline)
            .filter(StockKline.code == code)
            .order_by(StockKline.date.desc())
            .limit(2)
            .all()
        )
        if rows:
            latest = rows[0]
            out[code]["latest_date"] = latest.date.isoformat()
            out[code]["close"] = latest.close
            if len(rows) == 2 and rows[1].close:
                out[code]["pct_change"] = round((latest.close / rows[1].close - 1) * 100, 2)

        fin = (
            db.query(StockFinIndicator)
            .filter(StockFinIndicator.code == code)
            .order_by(StockFinIndicator.report_date.desc())
            .first()
        )
        if fin is not None:
            out[code]["roe"] = fin.roe

    return out
