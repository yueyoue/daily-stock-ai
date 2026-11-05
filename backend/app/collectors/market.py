"""大盘汇总采集（开发文档 §6 market_daily，任务 T1.9）。

涨跌家数 / 情绪原始分项（供 T7.3 情绪规则化打分取原始项）：
- 主源：东财全市场快照（stock_zh_a_spot_em）统计上涨/下跌/平盘、总成交额
- 备源：乐咕市场活跃度（stock_market_activity_legu）直接给家数

⚠️ 缺陷 D-01 修复：pandas 的 NaN 不会被 float() 抛错，直接累加会把 NaN
带进 extra（写 MySQL JSON 列报 3140）。此处一律走 safe_float/safe_int，
并把 extra 做 json_safe 清洗；快照中缺失涨跌幅的行（停牌等）不计入家数。
"""
import logging
from datetime import date as date_type

from app.collectors.base import BaseCollector
from app.collectors.util import (
    is_bad_number,
    json_safe,
    pick,
    safe_float,
    safe_int,
    to_date,
    to_records,
    to_records_indexed,
)

logger = logging.getLogger("app.collectors.market")

_DATE_KEYS = ("日期", "date")
_PCT_KEYS = ("涨跌幅", "changepercent")
_AMT_KEYS = ("成交额", "amount")
_UP_KEYS = ("上涨家数",)
_DOWN_KEYS = ("下跌家数",)
_FLAT_KEYS = ("平盘家数",)


class AkshareEastsSpotSummaryCollector(BaseCollector):
    """东财全市场快照统计主源。"""

    data_type = "market_summary"

    def fetch(self, **params) -> list[dict]:
        import akshare as ak

        run_date = to_date(params.get("date")) or date_type.today()
        rows = to_records(ak.stock_zh_a_spot_em())
        if not rows:
            raise RuntimeError("全市场快照为空")
        up = down = flat = 0
        turnover = 0.0
        for row in rows:
            pct = safe_float(pick(row, _PCT_KEYS))
            if pct is None:  # 停牌/缺失涨跌幅：不计入涨跌家数
                continue
            if pct > 0:
                up += 1
            elif pct < 0:
                down += 1
            else:
                flat += 1
            amount = safe_float(pick(row, _AMT_KEYS))
            if amount is not None:
                turnover += amount
        return [{"date": run_date, "up_count": up, "down_count": down,
                 "extra": json_safe({"flat_count": flat, "turnover_amount": turnover,
                                     "样本数": len(rows)})}]


class AkshareLeguActivityCollector(BaseCollector):
    """乐咕市场活跃度备源：直接给涨跌家数/赚钱效应等情绪原始分项。"""

    data_type = "market_summary"

    def fetch(self, **params) -> list[dict]:
        import akshare as ak

        run_date = to_date(params.get("date")) or date_type.today()
        rows = to_records_indexed(ak.stock_market_activity_legu())
        row = None
        for r in rows:
            if to_date(pick(r, _DATE_KEYS)) == run_date:
                row = r
                break
        if row is None and rows:
            row = rows[-1]  # 数据滞后时取最新一期
            logger.warning("乐咕活跃度无 %s 期数据，取最新一期", run_date)
        if row is None:
            raise RuntimeError("市场活跃度数据为空")

        extra = {}
        for key in ("赚钱效应", "真实涨跌比"):
            raw = pick(row, (key,))
            if not raw:
                continue
            value = safe_float(raw)
            if value is not None:
                extra[key] = value
            elif not is_bad_number(raw):  # "nan"/"inf" 等脏值丢弃，不留进 JSON
                extra[key] = raw
        flat_raw = pick(row, _FLAT_KEYS)
        if flat_raw:
            extra["flat_count"] = safe_int(flat_raw, 0)
        return [{
            "date": to_date(pick(row, _DATE_KEYS)) or run_date,
            "up_count": safe_int(pick(row, _UP_KEYS), 0),
            "down_count": safe_int(pick(row, _DOWN_KEYS), 0),
            "extra": json_safe(extra),
        }]