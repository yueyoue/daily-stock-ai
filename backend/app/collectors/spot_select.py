"""全市场快照本地筛选（T6.4 备源 / D-05 修复）。

背景：问财（pywencai）通道在部分网络环境不可达或被拦截，stock_select 线需要
一个不依赖第三方"语义查询"接口的自有备源——直接取 akshare 全市场快照，在本地
按涨跌幅/成交额/换手率等指标筛选并按涨跌幅排序取前 N，标注口径（非问财语义）。

标准化记录：{code, name, price, pct_change, amount, turnover_rate, market_cap}

参数（接口 params_template，调用方可覆盖）：
- top_n: 返回条数上限（默认 100）
- min_pct / max_pct: 涨跌幅区间（百分数，如 5 表示 >=5%）
- min_amount: 成交额下限（元）
- min_turnover: 换手率下限（百分数）
- exclude_st: 是否剔除 ST/退市股（默认 False）
- name_kw: 名称包含关键字（模糊过滤）
"""
from app.collectors.base import BaseCollector
from app.collectors.util import pick, safe_float, to_records

_CODE_KEYS = ("代码", "code")
_NAME_KEYS = ("名称", "name")
_PRICE_KEYS = ("最新价", "price")
_PCT_KEYS = ("涨跌幅", "changepercent")
_AMOUNT_KEYS = ("成交额", "amount")
_TURNOVER_KEYS = ("换手率", "turnover_rate")
_MV_KEYS = ("总市值", "market_cap")


class AkshareSpotSelectCollector(BaseCollector):
    """全市场快照本地筛选（stock_select 备源；不依赖问财语义接口）。"""

    data_type = "stock_select"

    def fetch(self, **params) -> list[dict]:
        import akshare as ak

        top_n = int(safe_float(params.get("top_n"), 100) or 100)
        min_pct = safe_float(params.get("min_pct"))
        max_pct = safe_float(params.get("max_pct"))
        min_amount = safe_float(params.get("min_amount"))
        min_turnover = safe_float(params.get("min_turnover"))
        exclude_st = bool(params.get("exclude_st", False))
        name_kw = str(params.get("name_kw") or "").strip()

        rows = to_records(ak.stock_zh_a_spot_em())
        if not rows:
            raise RuntimeError("全市场快照为空（ak.stock_zh_a_spot_em）")

        out: list[dict] = []
        for row in rows:
            code = str(pick(row, _CODE_KEYS) or "").strip()
            name = str(pick(row, _NAME_KEYS) or "").strip()
            if not code or not name:
                continue
            if exclude_st and ("ST" in name.upper() or "退" in name):
                continue
            if name_kw and name_kw not in name:
                continue
            pct = safe_float(pick(row, _PCT_KEYS))
            if min_pct is not None and (pct is None or pct < min_pct):
                continue
            if max_pct is not None and (pct is None or pct > max_pct):
                continue
            amount = safe_float(pick(row, _AMOUNT_KEYS))
            if min_amount is not None and (amount is None or amount < min_amount):
                continue
            turnover = safe_float(pick(row, _TURNOVER_KEYS))
            if min_turnover is not None and (turnover is None or turnover < min_turnover):
                continue
            out.append({
                "code": code,
                "name": name,
                "price": safe_float(pick(row, _PRICE_KEYS)),
                "pct_change": pct,
                "amount": amount,
                "turnover_rate": turnover,
                "market_cap": safe_float(pick(row, _MV_KEYS)),
            })

        out.sort(
            key=lambda r: (r["pct_change"] if r["pct_change"] is not None else -1e18),
            reverse=True,
        )
        return out[:max(1, top_n)]