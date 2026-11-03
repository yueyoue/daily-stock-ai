"""财务指标 30 项计算（T2.1，开发文档 §4 模块2）。

- INDICATOR_SPEC：30 项指标清单与口径常量（口径全文见同目录 fin_indicators.md，
  该清单同时是 T5.1 选股器财务因子的对齐基准）
- compute_fin_indicators_for_code：按**报告期粒度**计算（同比/环比需要同标的
  相邻报告期数据，一次加载整段历史内存内计算）
- sync_fin_indicators：**增量计算**——只写新增/变化的报告期，重复跑幂等不重复写
- 结果写 stock_fin_indicator：roe/gross_margin/net_margin/debt_to_asset/
  ocf_to_profit 五个定名列 + metrics JSON（30 项全量，供 T2.5 查询与 T5.x 选股）

口径要点（详见 fin_indicators.md）：
- 财报为累计口径；同比 = 本期累计 vs 去年同报告期累计；环比 = 单季值环比
  （单季 = 本期累计 - 上一报告期累计，中间缺期则环比置 None）
- 比率类单位为 %（净资产收益率 12.6 = 12.6%）；流动/速动比率为倍数
- 零分母/缺原始项 → None（宁缺毋错），不抛异常
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy.orm import Session

from app.models.market import StockFinance, StockFinIndicator

logger = logging.getLogger("app.fin_indicators")

# ---------- 30 项指标清单（固化；key/名称/单位/口径摘要） ----------
INDICATOR_SPEC: list[dict] = [
    # -- 规模（三大报表直取，单位元） --
    {"key": "total_revenue", "name": "营业总收入", "unit": "元", "formula": "利润表直取（累计口径）"},
    {"key": "net_profit", "name": "净利润", "unit": "元", "formula": "利润表直取（归母口径，累计）"},
    {"key": "deduct_net_profit", "name": "扣非净利润", "unit": "元", "formula": "利润表 extra 直取（缺原始项为 None）"},
    {"key": "operating_cashflow", "name": "经营现金流净额", "unit": "元", "formula": "现金流量表直取（累计）"},
    {"key": "invest_cashflow", "name": "投资现金流净额", "unit": "元", "formula": "现金流量表 extra 直取（缺原始项为 None）"},
    {"key": "finance_cashflow", "name": "筹资现金流净额", "unit": "元", "formula": "现金流量表 extra 直取（缺原始项为 None）"},
    {"key": "total_assets", "name": "总资产", "unit": "元", "formula": "资产负债表直取（期末）"},
    {"key": "total_liabilities", "name": "总负债", "unit": "元", "formula": "资产负债表直取（期末）"},
    {"key": "equity", "name": "净资产", "unit": "元", "formula": "资产负债表直取（归母权益，期末）"},
    # -- 每股（元/股） --
    {"key": "eps", "name": "每股收益", "unit": "元", "formula": "利润表直取（基本 EPS，累计）"},
    {"key": "bvps", "name": "每股净资产", "unit": "元", "formula": "净资产 / 股本（面值 1 元口径）"},
    {"key": "ocf_ps", "name": "每股经营现金流", "unit": "元", "formula": "经营现金流净额 / 股本"},
    # -- 盈利能力（%） --
    {"key": "gross_margin", "name": "毛利率", "unit": "%", "formula": "(营业总收入 - 营业成本) / 营业总收入 × 100"},
    {"key": "net_margin", "name": "净利率", "unit": "%", "formula": "净利润 / 营业总收入 × 100"},
    {"key": "operating_margin", "name": "营业利润率", "unit": "%", "formula": "营业利润 / 营业总收入 × 100"},
    {"key": "roe", "name": "净资产收益率", "unit": "%", "formula": "净利润 / 期末净资产 × 100（摊薄、累计口径）"},
    {"key": "roa", "name": "总资产收益率", "unit": "%", "formula": "净利润 / 期末总资产 × 100"},
    # -- 偿债与结构（%） --
    {"key": "debt_to_asset", "name": "资产负债率", "unit": "%", "formula": "总负债 / 总资产 × 100"},
    {"key": "equity_ratio", "name": "净资产比率", "unit": "%", "formula": "净资产 / 总资产 × 100"},
    # -- 偿债（倍） --
    {"key": "current_ratio", "name": "流动比率", "unit": "倍", "formula": "流动资产合计 / 流动负债合计"},
    {"key": "quick_ratio", "name": "速动比率", "unit": "倍", "formula": "(流动资产合计 - 存货) / 流动负债合计"},
    # -- 现金流质量（%） --
    {"key": "ocf_to_profit", "name": "经营现金流/净利润", "unit": "%", "formula": "经营现金流净额 / 净利润 × 100（盈利含金量）"},
    {"key": "ocf_to_revenue", "name": "经营现金流/营收", "unit": "%", "formula": "经营现金流净额 / 营业总收入 × 100"},
    {"key": "deduct_ratio", "name": "扣非净利/净利", "unit": "%", "formula": "扣非净利润 / 净利润 × 100"},
    {"key": "rd_expense_ratio", "name": "研发费用率", "unit": "%", "formula": "研发费用 / 营业总收入 × 100"},
    # -- 成长（%） --
    {"key": "revenue_yoy", "name": "营收同比增长率", "unit": "%", "formula": "(本期累计营收 - 去年同报告期营收) / |去年同报告期营收| × 100"},
    {"key": "profit_yoy", "name": "净利润同比增长率", "unit": "%", "formula": "(本期累计净利 - 去年同报告期净利) / |去年同报告期净利| × 100"},
    {"key": "ocf_yoy", "name": "经营现金流同比增长率", "unit": "%", "formula": "(本期累计现金流 - 去年同报告期现金流) / |去年同报告期现金流| × 100"},
    {"key": "revenue_qoq", "name": "营收单季环比", "unit": "%", "formula": "(本单季营收 - 上单季营收) / |上单季营收| × 100；单季=本期累计-上一报告期累计"},
    {"key": "profit_qoq", "name": "净利润单季环比", "unit": "%", "formula": "(本单季净利 - 上单季净利) / |上单季净利| × 100；单季口径同上"},
]

METRIC_KEYS: tuple[str, ...] = tuple(m["key"] for m in INDICATOR_SPEC)
NOMINAL_COLUMNS: tuple[str, ...] = ("roe", "gross_margin", "net_margin",
                                    "debt_to_asset", "ocf_to_profit")

# extra JSON 原始项别名（东财英文列 / 新浪中文行项目，先到先得；T1.8 口径）
_EXTRA_ALIASES: dict[str, tuple[str, ...]] = {
    "cost": ("OPERATE_COST", "营业成本"),
    "operate_profit": ("OPERATE_PROFIT", "营业利润"),
    "deduct_profit": ("DEDUCT_PARENT_NETPROFIT", "扣非净利润",
                      "扣除非经常性损益后的净利润"),
    "current_assets": ("TOTAL_CURRENT_ASSETS", "流动资产合计"),
    "current_liab": ("TOTAL_CURRENT_LIAB", "流动负债合计"),
    "inventory": ("INVENTORY", "存货"),
    "share_capital": ("TOTAL_SHARE", "SHARE_CAPITAL", "股本"),
    "rd_expense": ("RESEARCH_EXPENSE", "研发费用"),
    "invest_cashflow": ("NETCASH_INVEST", "NETCASH_INVEST_ACT",
                        "投资活动产生的现金流量净额"),
    "finance_cashflow": ("NETCASH_FINANCE", "NETCASH_FINANCE_ACT",
                         "筹资活动产生的现金流量净额"),
}

_ROUND = 4  # 指标小数位（口径文档同步）


# ---------- 工具 ----------

def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pick_extra(extra: dict, field: str) -> float | None:
    for key in _EXTRA_ALIASES[field]:
        if key in (extra or {}):
            v = _num(extra[key])
            if v is not None:
                return v
    return None


def _ratio(num: float | None, den: float | None, pct: bool = True) -> float | None:
    """比值：分母为 0/缺失 → None（宁缺毋错）；pct=True 输出百分数。"""
    if num is None or den is None or den == 0:
        return None
    v = num / den
    return round(v * 100, _ROUND) if pct else round(v, _ROUND)


def _growth(cur: float | None, base: float | None) -> float | None:
    """同比增长率（%）：基数缺失/为 0 → None；基数可为负（取绝对值作分母）。"""
    if cur is None or base is None or base == 0:
        return None
    return round((cur - base) / abs(base) * 100, _ROUND)


def _is_prev_quarter(prev: date, cur: date) -> bool:
    """prev 是否为 cur 的紧邻上一报告期（3/6/9/12 月季末序列）。"""
    return (cur.year == prev.year and cur.month - prev.month == 3) or (
        prev.month == 12 and cur.month == 3 and cur.year == prev.year + 1
    )


def _single_quarter(cur_cum: float | None, prev_row: StockFinance | None,
                    cur_date: date, prev_cum_getter) -> float | None:
    """单季值 = 本期累计 - 上一报告期累计（Q1 即单季；中间缺期 → None）。"""
    if cur_cum is None:
        return None
    if cur_date.month == 3:
        return cur_cum  # Q1 累计即单季
    if prev_row is None or not _is_prev_quarter(prev_row.report_date, cur_date):
        return None
    prev_cum = prev_cum_getter(prev_row)
    if prev_cum is None:
        return None
    return cur_cum - prev_cum


# ---------- 计算 ----------

def _metrics_for_rows(rows: list[StockFinance]) -> list[dict]:
    """整段报告期序列 → 每期 30 项指标（同比/环比在序列内取邻期）。"""
    by_date = {r.report_date: r for r in rows}
    out: list[dict] = []
    singles: dict[date, dict] = {}

    for i, row in enumerate(rows):
        prev = rows[i - 1] if i > 0 else None
        try:
            year_ago = by_date.get(row.report_date.replace(year=row.report_date.year - 1))
        except ValueError:  # 2/29
            year_ago = None

        rev = _num(row.total_revenue)
        np_ = _num(row.net_profit)
        ocf = _num(row.operating_cashflow)
        ta = _num(row.total_assets)
        tl = _num(row.total_liabilities)
        eq = _num(row.equity)
        eps = _num(row.eps)
        extra = row.extra or {}

        cost = _pick_extra(extra, "cost")
        op = _pick_extra(extra, "operate_profit")
        ded = _pick_extra(extra, "deduct_profit")
        ca = _pick_extra(extra, "current_assets")
        cl = _pick_extra(extra, "current_liab")
        inv = _pick_extra(extra, "inventory")
        sh = _pick_extra(extra, "share_capital")
        rd = _pick_extra(extra, "rd_expense")
        icf = _pick_extra(extra, "invest_cashflow")
        fcf = _pick_extra(extra, "finance_cashflow")

        # 单季值（环比用）：本期与上一报告期的累计差
        sq_rev = _single_quarter(rev, prev, row.report_date, lambda r: _num(r.total_revenue))
        sq_np = _single_quarter(np_, prev, row.report_date, lambda r: _num(r.net_profit))
        singles[row.report_date] = {"rev": sq_rev, "np": sq_np}

        prev_single = singles.get(prev.report_date) if prev else None
        yoy_rev = yoy_np = yoy_ocf = None
        if year_ago is not None:
            yoy_rev = _growth(rev, _num(year_ago.total_revenue))
            yoy_np = _growth(np_, _num(year_ago.net_profit))
            yoy_ocf = _growth(ocf, _num(year_ago.operating_cashflow))

        metrics = {
            "total_revenue": _round(rev),
            "net_profit": _round(np_),
            "deduct_net_profit": _round(ded),
            "operating_cashflow": _round(ocf),
            "invest_cashflow": _round(icf),
            "finance_cashflow": _round(fcf),
            "total_assets": _round(ta),
            "total_liabilities": _round(tl),
            "equity": _round(eq),
            "eps": _round(eps),
            "bvps": _round(_div(eq, sh)),
            "ocf_ps": _round(_div(ocf, sh)),
            "gross_margin": _ratio(None if cost is None or rev is None else rev - cost, rev),
            "net_margin": _ratio(np_, rev),
            "operating_margin": _ratio(op, rev),
            "roe": _ratio(np_, eq),
            "roa": _ratio(np_, ta),
            "debt_to_asset": _ratio(tl, ta),
            "equity_ratio": _ratio(eq, ta),
            "current_ratio": _ratio(ca, cl, pct=False),
            "quick_ratio": _ratio(None if inv is None or ca is None else ca - inv, cl, pct=False),
            "ocf_to_profit": _ratio(ocf, np_),
            "ocf_to_revenue": _ratio(ocf, rev),
            "deduct_ratio": _ratio(ded, np_),
            "rd_expense_ratio": _ratio(rd, rev),
            "revenue_yoy": yoy_rev,
            "profit_yoy": yoy_np,
            "ocf_yoy": yoy_ocf,
            "revenue_qoq": _growth(sq_rev, (prev_single or {}).get("rev")) if prev_single else None,
            "profit_qoq": _growth(sq_np, (prev_single or {}).get("np")) if prev_single else None,
        }
        assert set(metrics) == set(METRIC_KEYS)  # 清单固化：keys 与 INDICATOR_SPEC 对齐
        out.append({"code": row.code, "report_date": row.report_date, "metrics": metrics})
    return out


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, _ROUND)


def _div(a: float | None, b: float | None) -> float | None:
    return None if a is None or b in (None, 0) else a / b


def compute_fin_indicators_for_code(db: Session, code: str) -> list[dict]:
    """计算单标的全部报告期的 30 项指标（不落库，返回计算结果）。"""
    rows = (
        db.query(StockFinance)
        .filter(StockFinance.code == code)
        .order_by(StockFinance.report_date.asc())
        .all()
    )
    return _metrics_for_rows(rows)


# ---------- 增量落库 ----------

def sync_fin_indicators(db: Session, code: str) -> dict:
    """增量计算并写 stock_fin_indicator：只写新增/变化的报告期（重复跑幂等）。"""
    items = compute_fin_indicators_for_code(db, code)
    written = skipped = 0
    for item in items:
        metrics = item["metrics"]
        row = db.get(StockFinIndicator, (code, item["report_date"]))
        if row is not None and (row.metrics or {}) == metrics:
            skipped += 1  # 增量：输入未变 → 不重复写
            continue
        if row is None:
            row = StockFinIndicator(code=code, report_date=item["report_date"])
            db.add(row)
        for col in NOMINAL_COLUMNS:
            setattr(row, col, metrics.get(col))
        row.metrics = metrics
        written += 1
    db.commit()
    logger.info("[%s] 财务指标：计算 %d 期，写入 %d，跳过 %d", code, len(items), written, skipped)
    return {"code": code, "periods": len(items), "written": written, "skipped": skipped}


def sync_fin_indicators_all(db: Session) -> dict:
    """全市场批量增量计算（调度器批处理入口）。"""
    codes = [r[0] for r in db.query(StockFinance.code).distinct().all()]
    totals = {"codes": len(codes), "periods": 0, "written": 0, "skipped": 0}
    for code in codes:
        stats = sync_fin_indicators(db, code)
        for k in ("periods", "written", "skipped"):
            totals[k] += stats[k]
    return totals
