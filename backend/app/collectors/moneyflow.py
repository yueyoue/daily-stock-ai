"""资金流向采集（开发文档 §2.4.3 资金流向 / §6 market_daily，任务 T1.9）。

主力资金（东财口径，QPS≤1 防风控）：
- 主源：东财大盘资金流（stock_market_fund_flow）取当日市场主力净流入合计
- 备源：东财个股资金排行加总（stock_individual_fund_flow_rank，计算兜底）
⚠️ 不采北向净流入（季度披露不可得，开发文档 §10.7）。

⚠️ 缺陷 D-02 修复：akshare≥1.20 起 `stock_fund_flow_market` 已下线（模块无此属性），
主源改用 `stock_market_fund_flow()`（无参；列含「日期 / 主力净流入-净额 / 超大单净流入-净额」）。
按 run_date 匹配该行，未命中则取最新一期并在 extra 标注实际数据日期（不静默返空）。
"""
import logging
from datetime import date as date_type

from app.collectors.base import BaseCollector
from app.collectors.util import pick, safe_float, to_date, to_records, to_records_indexed

logger = logging.getLogger("app.collectors.moneyflow")

_DATE_KEYS = ("日期", "date")
_MAIN_KEYS = ("主力净流入-净额", "今日主力净流入-净额", "main_moneyflow")
_SUPER_KEYS = ("超大单净流入-净额", "今日超大单净流入-净额")


class AkshareEastsMarketFundFlowCollector(BaseCollector):
    """东财大盘资金流主源：市场主力净流入（元）。"""

    data_type = "moneyflow"

    def fetch(self, **params) -> list[dict]:
        import akshare as ak

        run_date = to_date(params.get("date")) or date_type.today()
        rows = to_records_indexed(ak.stock_market_fund_flow())
        matched = None       # 与 run_date 同日的那一行
        latest = None        # 全表最新一期（兜底）
        latest_date = None
        for row in rows:
            d = to_date(pick(row, _DATE_KEYS))
            if d is None:
                continue
            if latest_date is None or d > latest_date:
                latest, latest_date = row, d
            if d == run_date:
                matched = row
                break
        target = matched if matched is not None else latest
        if target is None:
            return []
        value = safe_float(pick(target, _MAIN_KEYS))
        if value is None:
            return []
        extra = {}
        sup = safe_float(pick(target, _SUPER_KEYS))
        if sup is not None:
            extra["超大单净流入"] = sup
        if matched is None:
            extra["数据口径"] = f"未含 {run_date} 数据，取最新一期 {latest_date}"
        return [{"date": run_date, "main_moneyflow": value, "extra": extra}]


class AkshareEastsFundFlowRankCollector(BaseCollector):
    """东财个股资金排行加总备源（计算兜底）。"""

    data_type = "moneyflow"

    def fetch(self, **params) -> list[dict]:
        import akshare as ak

        run_date = to_date(params.get("date")) or date_type.today()
        rows = to_records(ak.stock_individual_fund_flow_rank(indicator="今日"))
        total = 0.0
        counted = 0
        for row in rows:
            value = safe_float(pick(row, _MAIN_KEYS))
            if value is not None:
                total += value
                counted += 1
        if not counted:
            raise RuntimeError("个股资金排行取数为空")
        return [{"date": run_date, "main_moneyflow": total,
                 "extra": {"口径": f"个股排行加总({counted}只，兜底)"}}]
