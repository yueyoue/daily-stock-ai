"""估值快照服务（边风炜估值模型 · 第 1 层）。

口径原则（与开发文档 §4 模块4 一致，宁缺毋错）：
- **能直取就不自算**：市值、PB 用库内原始项推导，PE 同时给出「静态」与「年化」两种口径，
  并用 `pe_type` 标注，禁止混用（东财"市盈率-动态"与腾讯 TTM 实测可差 4%~11%，口径必须固定）
- 价格用**不复权 close**（与 kline/回测口径一致）；股息率用最近 12 个月每股股利（缺数据返回 None）
- 财务口径 = 最近报告期（≤ trade_date）carry-forward；累计数按季度数年化
- 任何分母缺失 → None，**不猜、不补 0**；调用方据此降级（跳过门槛并标注），不得当作通过

数据来源（均已在库）：
- StockKline.close（不复权价）
- StockFinIndicator: eps / bvps / revenue / net_profit / operating_cashflow / equity 等（metrics JSON）
- StockFinance.extra: 每股股利（多键探测，取不到为 None）
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from app.models.market import StockFinance, StockFinIndicator, StockInfo, StockKline
from app.services.fin_indicators import _pick_extra  # noqa: F401  复用别名口径

logger = logging.getLogger("app.valuation")

# 每股股利键名探测（东财 / 新浪 / 通用，命中即用）
_DPS_KEYS = (
    "DIVIDEND_PER_SHARE", "每股股利", "每股现金股利", "DPS",
    "ASSIGN_DIVIDEND_PORFIT", "现金分红比例",
)
# 总股本键名
_SHARE_KEYS = ("TOTAL_SHARE", "SHARE_CAPITAL", "股本", "总股本")


def _num(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None  # 过滤 NaN


def _pick(extra: dict | None, keys: tuple[str, ...]) -> float | None:
    for k in keys:
        if extra and k in extra:
            v = _num(extra[k])
            if v is not None:
                return v
    return None


def quarters(report_date: date) -> int:
    """报告期对应累计季度数：3→1、6→2、9→3、12→4。"""
    return max(1, report_date.month // 3)


def annualize(value: float | None, report_date: date | None) -> float | None:
    """累计口径年化（× 4 ÷ 季度数）；缺失返回 None。"""
    if value is None or report_date is None:
        return None
    return value * 4.0 / quarters(report_date)


def latest_trade_date(db: Session) -> date | None:
    return db.query(func.max(StockKline.date)).scalar()


def _close_map(db: Session, trade_date: date | None) -> tuple[dict[str, float], date | None]:
    """全市场最新收盘价（≤ trade_date）。返回 {code: close} 与实际日期。"""
    d = trade_date or latest_trade_date(db)
    if d is None:
        return {}, None
    # 先取该日或该日之前最近一个交易日
    actual = (db.query(func.max(StockKline.date))
              .filter(StockKline.date <= d).scalar())
    if actual is None:
        return {}, None
    rows = db.query(StockKline.code, StockKline.close).filter(StockKline.date == actual).all()
    return {c: float(close) for c, close in rows if close}, actual


def _fin_map(db: Session, codes: list[str], trade_date: date | None) -> dict[str, dict]:
    """每只票最新报告期指标（≤ trade_date，carry-forward）。"""
    out: dict[str, dict] = {}
    for i in range(0, len(codes), 500):
        chunk = codes[i:i + 500]
        q = db.query(StockFinIndicator).filter(StockFinIndicator.code.in_(chunk))
        if trade_date is not None:
            q = q.filter(StockFinIndicator.report_date <= trade_date)
        rows = q.order_by(StockFinIndicator.code.asc(),
                          desc(StockFinIndicator.report_date)).all()
        for r in rows:
            if r.code in out:
                continue
            m = dict(r.metrics or {})
            if r.roe is not None:
                m.setdefault("roe", r.roe)
            if r.gross_margin is not None:
                m.setdefault("gross_margin", r.gross_margin)
            if r.net_margin is not None:
                m.setdefault("net_margin", r.net_margin)
            if r.debt_to_asset is not None:
                m.setdefault("debt_to_asset", r.debt_to_asset)
            if r.ocf_to_profit is not None:
                m.setdefault("ocf_to_profit", r.ocf_to_profit)
            m["_report_date"] = r.report_date
            out[r.code] = m
    return out


def _extra_map(db: Session, codes: list[str], trade_date: date | None) -> dict[str, dict]:
    """每只票最新报告期的 extra（供每股股利/股本探测）。"""
    out: dict[str, dict] = {}
    for i in range(0, len(codes), 500):
        chunk = codes[i:i + 500]
        q = db.query(StockFinance).filter(StockFinance.code.in_(chunk))
        if trade_date is not None:
            q = q.filter(StockFinance.report_date <= trade_date)
        rows = q.order_by(StockFinance.code.asc(), desc(StockFinance.report_date)).all()
        for r in rows:
            if r.code in out:
                continue
            out[r.code] = r.extra or {}
    return out


def build_snapshot(db: Session, codes: list[str] | None = None,
                   trade_date: date | None = None) -> dict:
    """构建估值快照：{code: {...估值字段...}}。

    返回结构：
    {
      "meta": {"trade_date": "2026-11-20", "rows": 5142, "pe_type": "annualized", "missing": {...}},
      "rows": {code: {"price","pe_static","pe_annualized","pb","ps","pcf","mkt_cap",
                      "div_yield","eps","bvps","report_date"}}
    }
    """
    closes, actual = _close_map(db, trade_date)
    if codes is None:
        codes = sorted(closes)
    else:
        codes = [c for c in codes if c in closes]
    fin = _fin_map(db, codes, trade_date)
    extra = _extra_map(db, codes, trade_date)

    rows: dict[str, dict] = {}
    miss = {"price": 0, "eps": 0, "bvps": 0, "share": 0, "dps": 0}
    for code in codes:
        price = closes.get(code)
        m = fin.get(code) or {}
        rd = m.get("_report_date")
        if price is None:
            miss["price"] += 1
            continue
        if not m:
            miss["eps"] += 1
            continue
        q = quarters(rd) if isinstance(rd, date) else 1
        eps = _num(m.get("eps"))
        bvps = _num(m.get("bvps"))
        ocf_ps = _num(m.get("ocf_ps"))
        equity = _num(m.get("equity"))
        revenue = _num(m.get("total_revenue"))
        net_profit = _num(m.get("net_profit"))
        ocf = _num(m.get("operating_cashflow"))

        # 总股本：优先 extra 总股本，其次 equity/bvps 反推（两者皆原始项，不引入额外假设）
        share = _pick(extra.get(code), _SHARE_KEYS)
        if not share and equity and bvps and bvps > 0:
            share = equity / bvps
        if not share:
            miss["share"] += 1
        mkt_cap = price * share if share else None

        eps_ann = eps * 4.0 / q if eps is not None else None
        rev_ann = annualize(revenue, rd)
        ocf_ann = annualize(ocf, rd)
        np_ann = annualize(net_profit, rd)

        dps = _pick(extra.get(code), _DPS_KEYS)
        if dps is None:
            miss["dps"] += 1

        rows[code] = {
            "price": price,
            "report_date": rd.isoformat() if isinstance(rd, date) else None,
            "eps": eps,
            "bvps": bvps,
            "ocf_ps": ocf_ps,
            "share": share,
            "mkt_cap": mkt_cap,
            # 两种 PE 口径并存，调用方必须看 pe_type
            "pe_static": (price / eps) if eps and eps > 0 else None,
            "pe_annualized": (price / eps_ann) if eps_ann and eps_ann > 0 else None,
            "pb": (price / bvps) if bvps and bvps > 0 else None,
            "ps": (mkt_cap / rev_ann) if mkt_cap and rev_ann and rev_ann > 0 else None,
            "pcf": (mkt_cap / ocf_ann) if mkt_cap and ocf_ann and ocf_ann > 0 else None,
            "ocf_yield": (ocf_ann / mkt_cap) if mkt_cap and ocf_ann else None,
            "div_yield": (dps / price) if dps and price else None,
            "net_margin_ann": (np_ann / rev_ann) if np_ann and rev_ann else None,
            "debt_to_asset": _num(m.get("debt_to_asset")),
            "roe": _num(m.get("roe")),
            "revenue_yoy": _num(m.get("revenue_yoy")),
            "profit_yoy": _num(m.get("profit_yoy")),
            "ocf_to_profit": _num(m.get("ocf_to_profit")),
            "deduct_ratio": _num(m.get("deduct_ratio")),
            "rd_expense_ratio": _num(m.get("rd_expense_ratio")),
            # 可信度 gate 所需（bw_model.GATE_RULES）
            "ocf_yoy": _num(m.get("ocf_yoy")),
            "quick_ratio": _num(m.get("quick_ratio")),
            "gross_margin": _num(m.get("gross_margin")),
            "net_margin": _num(m.get("net_margin")),
        }
    meta = {
        "trade_date": actual.isoformat() if actual else None,
        "rows": len(rows),
        "pe_type": "annualized",  # 默认口径：最新报告期年化（与东财"动态"同源），静态另列
        "missing": miss,
        "note": "pe_annualized=最新报告期年化；pe_static=最近年报；两者不可混用（实测两源差 4%~11%）",
    }
    return {"meta": meta, "rows": rows}


def industry_map(db: Session, codes: list[str]) -> dict[str, tuple[str, str]]:
    """{code: (name, industry)}。"""
    out: dict[str, tuple[str, str]] = {}
    for i in range(0, len(codes), 500):
        chunk = codes[i:i + 500]
        for c, n, ind in (db.query(StockInfo.code, StockInfo.name, StockInfo.industry)
                          .filter(StockInfo.code.in_(chunk)).all()):
            out[c] = (n or "", ind or "")
    return out