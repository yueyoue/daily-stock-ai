"""资金流向采集（开发文档 §2.4.3 资金流向 / §6 market_daily，任务 T1.9）。

主力资金（东财口径，QPS≤1 防风控）：
- 主源：东财大盘资金流（stock_fund_flow_market）取当日市场主力净流入合计
- 备源：东财个股资金排行加总（stock_individual_fund_flow_rank，计算兜底）
⚠️ 不采北向净流入（季度披露不可得，开发文档 §10.7）。
"""
import logging
from datetime import date as date_type

from app.collectors.base import BaseCollector
from app.collectors.util import pick, to_date, to_records, to_records_indexed

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
        rows = to_records_indexed(ak.stock_fund_flow_market(symbol="沪深A股"))
        out = []
        for row in rows:
            d = to_date(pick(row, _DATE_KEYS)) or run_date
            if d != run_date:
                continue
            value = pick(row, _MAIN_KEYS)
            if not value:
                continue
            extra = {}
            sup = pick(row, _SUPER_KEYS)
            if sup:
                extra["超大单净流入"] = float(sup)
            out.append({"date": d, "main_moneyflow": float(value), "extra": extra})
        return out


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
            value = pick(row, _MAIN_KEYS)
            if value:
                try:
                    total += float(value)
                    counted += 1
                except ValueError:
                    continue
        if not counted:
            raise RuntimeError("个股资金排行取数为空")
        return [{"date": run_date, "main_moneyflow": total,
                 "extra": {"口径": f"个股排行加总({counted}只，兜底)"}}]
