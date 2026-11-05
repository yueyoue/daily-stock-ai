"""财务数据采集（开发文档 §5.2 / §6 stock_finance，任务 T1.8）。

三大报表关键项按**报告期**入库（10 年）：
- 主源 akshare-东财：stock_{balance,profit,cash_flow}_sheet_by_report_em 三表按报告期合并
- 备源 akshare-新浪：stock_financial_report_sina 三表（行项目×报告期 矩阵，转置解析）
- 接口登记见 services/source_seed.py；季度增量由调用方全量拉取+幂等 upsert 完成

关键科目（落列）：营收/净利/经营现金流/总资产/总负债/权益/EPS；
三表其余非空科目进 extra JSON（供 T2.1 财务指标 30 项计算，白名单外丢弃控制体积）。

⚠️ 缺陷 D-01 同类加固：所有数值取值统一走 safe_float，NaN/±Inf/空串一律归一为 None，
杜绝非标准 JSON 的 NaN 进入 stock_finance（Float 列）与 extra（JSON 列）。
"""
import logging

from app.collectors.base import BaseCollector
from app.collectors.util import is_bad_number, pick, safe_float, to_date, to_records

logger = logging.getLogger("app.collectors.finance")

# 报告期列别名
_DATE_KEYS = ("REPORT_DATE", "REPORT_DATE_NAME", "报告期", "date")

# 关键科目 → 候选列名（东财英文列 / 新浪中文行项目，先到先得）
FIELD_ALIASES: dict[str, tuple] = {
    "total_revenue": ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME", "营业总收入", "一、营业总收入"),
    "net_profit": ("PARENT_NETPROFIT", "NETPROFIT", "净利润", "归属于母公司股东的净利润"),
    "operating_cashflow": ("NETCASH_OPERATE", "NET_OPERATE_CASH_FLOW", "NETCASH_OPERATE_ACT",
                           "经营活动产生的现金流量净额"),
    "total_assets": ("TOTAL_ASSETS", "资产总计"),
    "total_liabilities": ("TOTAL_LIABILITIES", "负债合计"),
    "equity": ("TOTAL_PARENT_EQUITY", "TOTAL_EQUITY", "TOTAL_OWNER_EQUITY",
               "归属于母公司股东权益合计", "所有者权益合计"),
    "eps": ("BASIC_EPS", "EPS", "基本每股收益"),
}

EXTRA_MAX_FIELDS = 200  # extra JSON 体积护栏（每股×报告期）


def _to_float(value):
    """数值归一：NaN/±Inf/空串/"--" 等一律返回 None（委托 util.safe_float）。"""
    return safe_float(value)


def merge_statement_rows(sheets: dict[str, list[dict]]) -> list[dict]:
    """多表记录按报告期合并 → 标准化 stock_finance 记录（先到先得，空值不覆盖）。

    sheets: {"profit": [...], "balance": [...], "cashflow": [...]} 各表记录
    每条记录至少含报告期列（_DATE_KEYS 别名）+ 科目列。
    """
    merged: dict = {}
    for _sheet, rows in sheets.items():
        for row in rows:
            d = to_date(pick(row, _DATE_KEYS))
            if d is None:
                continue
            slot = merged.setdefault(d, {})
            for k, v in row.items():
                if v is not None and str(v).strip() != "" and k not in slot:
                    slot[k] = v

    out: list[dict] = []
    for d in sorted(merged):
        slot = merged[d]
        rec: dict = {"report_date": d, "report_type": "quarterly"}
        for field, aliases in FIELD_ALIASES.items():
            raw = pick(slot, aliases)
            rec[field] = safe_float(raw) if raw not in (None, "") else None
        extra: dict = {}
        for k, v in slot.items():
            if k in _DATE_KEYS:
                continue
            num = safe_float(v)
            if num is not None:
                extra[k] = num
                continue
            text = str(v).strip()
            if text and not is_bad_number(v):
                extra[k] = text[:64]
        rec["extra"] = dict(list(extra.items())[:EXTRA_MAX_FIELDS])
        out.append(rec)
    return out


class AkshareEastsFinanceCollector(BaseCollector):
    """akshare-东财三大报表（按报告期）主源。"""

    data_type = "finance"

    SHEETS = {
        "profit": "stock_profit_sheet_by_report_em",
        "balance": "stock_balance_sheet_by_report_em",
        "cashflow": "stock_cash_flow_sheet_by_report_em",
    }

    def fetch(self, **params) -> list[dict]:
        import akshare as ak

        code = str(params.get("code", "")).strip()
        if not code:
            raise ValueError("finance 采集缺少 code 参数")
        start_date = to_date(params.get("start_date"))
        end_date = to_date(params.get("end_date"))

        sheets: dict[str, list[dict]] = {}
        for key, fn_name in self.SHEETS.items():
            fn = getattr(ak, fn_name)
            sheets[key] = to_records(fn(symbol=code))

        records = merge_statement_rows(sheets)
        for rec in records:  # 采集器统一挂 code
            rec["code"] = code
        if start_date:
            records = [r for r in records if r["report_date"] >= start_date]
        if end_date:
            records = [r for r in records if r["report_date"] <= end_date]
        return records


class AkshareSinaFinanceCollector(BaseCollector):
    """akshare-新浪三大报表备源（行项目×报告期 矩阵，转置后走同一合并口径）。"""

    data_type = "finance"

    SHEETS = {"profit": "利润表", "balance": "资产负债表", "cashflow": "现金流量表"}

    def fetch(self, **params) -> list[dict]:
        import akshare as ak

        code = str(params.get("code", "")).strip()
        if not code:
            raise ValueError("finance 采集缺少 code 参数")
        start_date = to_date(params.get("start_date"))
        end_date = to_date(params.get("end_date"))
        stock = self._sina_stock(code)

        sheets: dict[str, list[dict]] = {}
        for key, sheet_name in self.SHEETS.items():
            df = ak.stock_financial_report_sina(stock=stock, symbol=sheet_name)
            sheets[key] = self._transpose(df)

        records = merge_statement_rows(sheets)
        for rec in records:
            rec["code"] = code
        if start_date:
            records = [r for r in records if r["report_date"] >= start_date]
        if end_date:
            records = [r for r in records if r["report_date"] <= end_date]
        return records

    @staticmethod
    def _transpose(df) -> list[dict]:
        """新浪报表矩阵转置：列=报告期、行=科目 → [{科目:值, ...报告期列}]。"""
        matrix = df.to_dict()  # {列名: {行索引(科目): 值}}
        out: list[dict] = []
        for col, items in matrix.items():
            if not isinstance(items, dict):
                continue
            row = {str(k).strip(): v for k, v in items.items()}
            row["REPORT_DATE"] = str(col)  # 列名即报告期
            out.append(row)
        return out

    @staticmethod
    def _sina_stock(code: str) -> str:
        if code.startswith("6"):
            return f"sh{code}"
        if code.startswith(("0", "3")):
            return f"sz{code}"
        return f"bj{code}"