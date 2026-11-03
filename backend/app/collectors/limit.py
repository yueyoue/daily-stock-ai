"""涨跌停采集（开发文档 §2.4.3 涨停/情绪统计，任务 T1.9）。

- 主源：东财涨跌停池（stock_zt_pool_em / stock_zt_pool_dtgc_em）——家数 + 连板梯队
- 备源：计算兜底（基于全市场快照涨跌幅 ±9.8% 近似，标注口径）
连板梯队基础数据进 market_daily.extra（T3.2/T4.3 涨停梯队展示用）。
"""
import logging
from collections import Counter
from datetime import date as date_type

from app.collectors.base import BaseCollector
from app.collectors.util import pick, to_date, to_records

logger = logging.getLogger("app.collectors.limit")

_CODE_KEYS = ("代码", "code")
_NAME_KEYS = ("名称", "name")
_LB_KEYS = ("连板数", "连板天数")
_PCT_KEYS = ("涨跌幅", "changepercent")


class AkshareEastsLimitPoolCollector(BaseCollector):
    """东财涨跌停池主源。"""

    data_type = "limit_updown"

    def fetch(self, **params) -> list[dict]:
        import akshare as ak

        run_date = to_date(params.get("date")) or date_type.today()
        date_arg = run_date.strftime("%Y%m%d")
        zt_rows = to_records(ak.stock_zt_pool_em(date=date_arg))
        dt_rows = to_records(ak.stock_zt_pool_dtgc_em(date=date_arg))

        lianban: Counter = Counter()
        top: list[dict] = []
        for row in zt_rows:
            try:
                n = int(float(pick(row, _LB_KEYS) or 1))
            except ValueError:
                n = 1
            lianban[n] += 1
            top.append({"code": pick(row, _CODE_KEYS), "name": pick(row, _NAME_KEYS),
                        "连板数": n})
        top.sort(key=lambda x: -x["连板数"])
        return [{
            "date": run_date,
            "limit_up_count": len(zt_rows),
            "limit_down_count": len(dt_rows),
            "extra": {
                "连板梯队": {f"{k}板": v for k, v in sorted(lianban.items())},
                "最高连板": max(lianban) if lianban else 0,
                "高连板股": top[:10],
            },
        }]


class AkshareSpotLimitCollector(BaseCollector):
    """计算兜底备源：基于全市场快照涨跌幅近似统计涨跌停家数。"""

    data_type = "limit_updown"

    def fetch(self, **params) -> list[dict]:
        import akshare as ak

        run_date = to_date(params.get("date")) or date_type.today()
        rows = to_records(ak.stock_zh_a_spot_em())
        if not rows:
            raise RuntimeError("全市场快照为空")
        up = down = 0
        for row in rows:
            try:
                pct = float(pick(row, _PCT_KEYS) or 0)
            except ValueError:
                continue
            if pct >= 9.8:
                up += 1
            elif pct <= -9.8:
                down += 1
        return [{"date": run_date, "limit_up_count": up, "limit_down_count": down,
                 "extra": {"涨跌停口径": "备源兜底：快照涨跌幅±9.8%近似（未按板块阈值细分）"}}]
