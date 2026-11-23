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
from datetime import date, timedelta

from sqlalchemy import bindparam, desc, func, text
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


# 财务窗口：最近约 18 个月（4~5 个季度 + 公告滞后），避免每次把 40 期历史全量搬一遍
FIN_WINDOW_DAYS = 550
# 最新交易日覆盖率保护：不足前一交易日的 80% 视为「当日未采完」，回退到前一交易日
DATE_MIN_RATIO = 0.8


def _window_start(trade_date: date | None, span_days: int = FIN_WINDOW_DAYS) -> date:
    return (trade_date or date.today()) - timedelta(days=span_days)


def _date_count(db: Session, d: date) -> int:
    return (db.query(func.count()).select_from(StockKline)
            .filter(StockKline.date == d).scalar() or 0)


def _complete_trade_date(db: Session, d: date) -> tuple[date, dict]:
    """最新交易日若只采到部分标的（< 前一交易日 80%），回退到前一交易日并记录口径。"""
    prev = (db.query(func.max(StockKline.date)).filter(StockKline.date < d).scalar())
    if prev is None:
        return d, {"fallback": False, "requested": d.isoformat(), "used": d.isoformat(), "coverage": None}
    cur_n, prev_n = _date_count(db, d), _date_count(db, prev)
    ratio = (cur_n / prev_n) if prev_n else None
    info = {"fallback": False, "requested": d.isoformat(), "used": d.isoformat(),
            "coverage": round(ratio, 3) if ratio is not None else None}
    if ratio is not None and ratio < DATE_MIN_RATIO:
        logger.warning("交易日 %s 日线仅覆盖 %.1f%%（%d/%d），回退到 %s",
                       d, ratio * 100, cur_n, prev_n, prev)
        info.update(fallback=True, used=prev.isoformat())
        return prev, info
    return d, info


def _close_map(db: Session, trade_date: date | None) -> tuple[dict[str, float], date | None, dict]:
    """全市场最新收盘价（≤ trade_date）。返回 {code: close}、实际日期与日期口径信息。"""
    if trade_date is None:
        d = latest_trade_date(db)  # MAX(date) 走 ix_stock_kline_date，毫秒级
    else:
        d = (db.query(func.max(StockKline.date))
             .filter(StockKline.date <= trade_date).scalar())
    if d is None:
        return {}, None, {"fallback": False, "requested": None, "used": None, "coverage": None}
    actual, date_info = _complete_trade_date(db, d)
    rows = db.query(StockKline.code, StockKline.close).filter(StockKline.date == actual).all()
    return {c: float(close) for c, close in rows if close}, actual, date_info


def _fin_map(db: Session, codes: list[str], trade_date: date | None) -> dict[str, dict]:
    """每只票最新报告期指标（≤ trade_date，carry-forward；只取窗口内，不再全量搬 40 期）。"""
    out: dict[str, dict] = {}
    start = _window_start(trade_date)
    for i in range(0, len(codes), 500):
        chunk = codes[i:i + 500]
        q = db.query(StockFinIndicator).filter(
            StockFinIndicator.code.in_(chunk),
            StockFinIndicator.report_date >= start,
        )
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
    """每只票最新报告期的总股本（读物化小表 stock_fin_extra_slim）。

    背景：stock_finance 单表 3.1GB，带 extra JSON 全表读出约 160s；小表 19 万行、
    约 10MB，按 (report_date, code) 索引扫描为毫秒级。小表由 /app/data/build_slim.py
    从 extra.SHARE_CAPITAL 物化，财报增量采集后可重跑刷新。
    表不存在时回退旧 JSON 读法（慢但可用），不因缺表直接 500。
    """
    start = _window_start(trade_date)
    cond = " AND report_date <= :end" if trade_date is not None else ""
    sql = text(
        "SELECT code, report_date, share_capital FROM stock_fin_extra_slim "
        "WHERE code IN :codes AND report_date >= :start" + cond +
        " ORDER BY code ASC, report_date DESC"
    ).bindparams(bindparam("codes", expanding=True))
    params: dict = {"start": start}
    if trade_date is not None:
        params["end"] = trade_date
    out: dict[str, dict] = {}
    try:
        for i in range(0, len(codes), 800):
            chunk = codes[i:i + 800]
            rows = db.execute(sql, {**params, "codes": chunk}).fetchall()
            for code, _rd, share in rows:
                if code in out:
                    continue
                out[code] = {"SHARE_CAPITAL": share} if share else {}
        return out
    except Exception as e:  # 表缺失/权限异常：回退旧读法
        db.rollback()
        logger.warning("物化表 stock_fin_extra_slim 不可用（%s），回退 extra JSON（慢）", e)
        return _extra_map_json(db, codes, trade_date)


def _extra_map_json(db: Session, codes: list[str], trade_date: date | None) -> dict[str, dict]:
    """旧读法：直接读 stock_finance.extra（3.1GB 大表，约 160s/次，仅作兜底）。"""
    out: dict[str, dict] = {}
    start = _window_start(trade_date)
    for i in range(0, len(codes), 500):
        chunk = codes[i:i + 500]
        q = db.query(StockFinance).filter(
            StockFinance.code.in_(chunk),
            StockFinance.report_date >= start,
        )
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
    closes, actual, date_info = _close_map(db, trade_date)
    if codes is None:
        codes = sorted(closes)
    else:
        codes = [c for c in codes if c in closes]
    fin = _fin_map(db, codes, trade_date)
    extra = _extra_map(db, codes, trade_date)

    # 兜底只在「财务确实覆盖不足」时启用：原 max(10, 10%) 对单票查询恒成立（1 < 10），
    # 导致每次点开详情都去请求东财快照并等 12s 超时
    spot: dict[str, dict] = {}
    coverage = (len(fin) / len(codes)) if codes else 0.0
    if codes and (not fin or (len(codes) >= 50 and coverage < 0.1)):
        spot = load_spot_snapshot()
        logger.info("财务覆盖不足（%d/%d），启用东财快照兜底 %d 只", len(fin), len(codes), len(spot))

    rows: dict[str, dict] = {}
    miss = {"price": 0, "eps": 0, "bvps": 0, "share": 0, "dps": 0}
    for code in codes:
        price = closes.get(code)
        m = fin.get(code) or {}
        sp = spot.get(code) or {}
        rd = m.get("_report_date")
        if price is None:
            price = sp.get("price")
        if price is None:
            miss["price"] += 1
            continue
        if not m and not sp:
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
        mkt_cap = price * share if share else sp.get('mkt_cap')

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
            "pe_annualized": (price / eps_ann) if eps_ann and eps_ann > 0 else sp.get("pe"),
            "pb": (price / bvps) if bvps and bvps > 0 else sp.get("pb"),
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
    source = "db" if fin and not spot else ("spot" if spot and not fin else ("mixed" if spot else "db"))
    meta = {
        "trade_date": actual.isoformat() if actual else None,
        "date_fallback": date_info.get("fallback"),
        "date_requested": date_info.get("requested"),
        "date_coverage": date_info.get("coverage"),
        "rows": len(rows),
        "source": source,
        "spot_rows": len(spot),
        "spot_stale": _SPOT_META.get("stale"),
        "spot_age_sec": _SPOT_META.get("age"),
        "spot_error": _SPOT_META.get("error"),
        "pe_type": "annualized",  # 默认口径：最新报告期年化（与东财"动态"同源），静态另列
        "missing": miss,
        "note": "pe_annualized=最新报告期年化；pe_static=最近年报；两者不可混用（实测两源差 4%~11%）",
    }
    return {"meta": meta, "rows": rows}


# ---------- 快照直取（财务数据缺失时的兜底） ----------
_SPOT_URL = "https://push2.eastmoney.com/api/qt/clist/get"
_SPOT_FIELDS = "f12,f14,f2,f9,f23,f20,f21"
_SPOT_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
_SPOT_CACHE = "/app/data/spot_valuation.json"
_SPOT_META: dict = {"stale": False, "age": None, "error": None, "rows": 0}
_SPOT_FAIL: dict = {"ts": 0.0}  # 上次抓取失败时间（用于退避）
SPOT_BACKOFF = 600  # 失败退避秒数：10 分钟内不重试


def _cache_file() -> str:
    import os
    import tempfile
    if os.path.isdir("/app/data"):
        return _SPOT_CACHE
    return os.path.join(tempfile.gettempdir(), "dsa_spot_valuation.json")


def fetch_spot_snapshot(timeout: float = 12.0) -> dict[str, dict]:
    """东财全市场快照：{code: {price, pe, pb, mkt_cap, float_cap}}。"""
    import json
    import urllib.request
    out: dict[str, dict] = {}
    for page in range(1, 61):
        url = (f"{_SPOT_URL}?pn={page}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f20"
               f"&fs={_SPOT_FS}&fields={_SPOT_FIELDS}")
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
        try:
            raw = urllib.request.urlopen(req, timeout=timeout).read()
            data = json.loads(raw.decode("utf-8", "ignore"))
        except Exception as e:
            logger.warning("东财快照第 %d 页失败：%s", page, e)
            break
        diff = ((data.get("data") or {}).get("diff")) or []
        if not diff:
            break
        for r in diff:
            code = str(r.get("f12") or "")
            if not code:
                continue
            out[code] = {
                "price": _num(r.get("f2")),
                "pe": _num(r.get("f9")),
                "pb": _num(r.get("f23")),
                "mkt_cap": _num(r.get("f20")),
                "float_cap": _num(r.get("f21")),
            }
        if len(diff) < 100:
            break
    logger.info("东财快照获取 %d 只", len(out))
    return out


def load_spot_snapshot(force: bool = False, ttl: int = 3600) -> dict[str, dict]:
    """读取快照：优先新鲜缓存；抓取失败时回退旧缓存（不因限流把页面打空）。"""
    import json
    import os
    import time

    path = _cache_file()
    cached = None
    age = None
    if os.path.exists(path):
        try:
            age = time.time() - os.path.getmtime(path)
            with open(path, encoding="utf-8") as fh:
                cached = json.load(fh)
        except Exception:
            cached = None

    # 失败退避：上次抓取失败后 SPOT_BACKOFF 内直接用旧缓存，不再请求数据源
    if cached and not force and _SPOT_FAIL["ts"] and (time.time() - _SPOT_FAIL["ts"]) < SPOT_BACKOFF:
        wait_min = int((time.time() - _SPOT_FAIL["ts"]) // 60)
        _SPOT_META.update(stale=True, age=round(age) if age else None,
                          error=f"数据源限流退避中（{wait_min} 分钟前失败，暂不重试）",
                          rows=len(cached))
        return cached

    if cached and not force and age is not None and age < ttl:
        _SPOT_META.update(stale=False, age=round(age), error=None, rows=len(cached))
        return cached

    data = fetch_spot_snapshot()
    if data:
        _SPOT_FAIL["ts"] = 0.0
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except Exception as e:
            logger.warning("快照缓存写入失败：%s", e)
        _SPOT_META.update(stale=False, age=0, error=None, rows=len(data))
        return data

    if cached:
        _SPOT_FAIL["ts"] = time.time()
        _SPOT_META.update(stale=True, age=round(age) if age else None,
                          error="实时抓取失败，使用上次缓存", rows=len(cached))
        logger.warning("东财快照抓取失败，回退旧缓存（%d 只，%s 秒前）", len(cached), round(age or 0))
        return cached

    _SPOT_META.update(stale=True, age=None, error="实时抓取失败且无可用缓存", rows=0)
    return {}


def industry_map(db: Session, codes: list[str]) -> dict[str, tuple[str, str]]:
    """{code: (name, industry)}。"""
    out: dict[str, tuple[str, str]] = {}
    for i in range(0, len(codes), 500):
        chunk = codes[i:i + 500]
        for c, n, ind in (db.query(StockInfo.code, StockInfo.name, StockInfo.industry)
                          .filter(StockInfo.code.in_(chunk)).all()):
            out[c] = (n or "", ind or "")
    return out