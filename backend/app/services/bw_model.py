"""边风炜估值选股模型（第 2 层：行业分档 + 硬门槛 + 加权评分 + 区间位置）。

设计取舍（对齐提炼出的方法，可回溯到「炜炜道来」30 期原话）：
- **分档**：同一倍数在不同行业含义不同（苹果链难过 20 倍 / AI 龙头难过 35 倍 /
  银行看 PB 1 倍），故先按行业映射风格，再由风格决定口径与阈值
- **硬门槛 + 软评分**：门槛复刻「一票否决」（PE≤20、股息率≥4%、净现比≥1），
  评分复刻「区间内不操作、越界才动手」；输出三档结论（pass/watch/reject）而非买入名单
- **第 0 层 gate**：财报可信度不达标直接排除（「财报不能信就不给估值」），不参与评分
- **宁缺毋错**：某门槛所需数据缺失时**跳过并标注**（skip），不当作通过、也不当作失败
- **阈值可配**：全部阈值集中在此文件的 RULES/WEIGHTS，便于按年份调整并留版本记录

局限（必须如实告知，写进 API 的 notes）：
- 好生意/护城河/行业景气/能力圈无法量化，只作人工复核项
- 保险需要 EV/NBV，当前无数据线 → insurance 规则默认以 PB+股息率+ROE 近似，标注 degraded
- 成长股 PE 上限对无利润公司无意义 → 缺利润时改看 PS（ps_to_growth）
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy.orm import Session

from app.services.valuation import build_snapshot, industry_map

logger = logging.getLogger("app.bw_model")

MODEL_VERSION = "bw-value-v1.0"

# 无风险利率（%）：股息率门槛的基准，可在系统配置覆盖
DEFAULT_RISK_FREE = 1.8
# 股息率溢价（百分点）：「红利三条件」里的 国债 + 2
DIV_PREMIUM = 2.0

# ---------- 行业 → 风格映射（关键词匹配，可按需覆盖） ----------
STYLE_MAP: list[tuple[str, tuple[str, ...]]] = [
    ("bank", ("银行",)),
    ("insurance", ("保险",)),
    ("property", ("房地产", "地产")),
    ("utility", ("电力", "燃气", "水务", "公用", "高速", "港口", "电信", "通信服务")),
    ("cyclical", ("煤炭", "有色", "钢铁", "化工", "石油", "化纤", "建材", "水泥", "玻璃", "航运")),
    ("consumer", ("白酒", "酿酒", "食品", "饮料", "家电", "白色家电", "商业", "零售", "纺织", "服装", "旅游", "酒店", "医美")),
    ("growth", ("半导体", "电子", "计算机", "软件", "通信", "医药", "生物", "医疗", "电力设备", "电池", "光伏",
                "汽车", "机械", "军工", "传媒", "游戏", "互联网", "安防", "元件", "光学", "消费电子")),
]

STYLE_LABEL = {
    "bank": "银行", "insurance": "保险", "property": "地产", "utility": "红利/公用",
    "cyclical": "周期", "consumer": "消费", "growth": "成长", "other": "未分类",
}

# ---------- 各风格规则（hard 一票否决；soft 参与评分；band 用于区间位置） ----------
# hard 项：(因子, 比较符, 阈值, 权重说明)  比较符同选股 DSL
RULES: dict[str, dict] = {
    "bank": {
        "hard": [
            ("pb", "between", (0.4, 1.2)),
            ("div_yield", ">=", 4.0),
            ("roe", ">=", 9.0),
            ("revenue_yoy", ">=", 0.0),
        ],
        "band": ("pb", 0.4, 1.2),
        "note": "PB 分档 + 股息率：拨备/不良无数据线，以 ROE 与增速近似",
    },
    "utility": {
        "hard": [
            ("div_yield", ">=", DEFAULT_RISK_FREE + DIV_PREMIUM),
            ("ocf_to_profit", ">=", 0.9),
            ("revenue_yoy", ">=", -5.0),
        ],
        "band": ("pe_annualized", 10.0, 22.0),
        "note": "红利三条件：分红可持续、股息率≥国债+2、营收利润不塌",
    },
    "consumer": {
        "hard": [
            ("pe_annualized", "between", (8.0, 25.0)),
            ("div_yield", ">=", 2.5),
            ("deduct_ratio", ">=", 85.0),
            ("ocf_to_profit", ">=", 0.8),
        ],
        "band": ("pe_annualized", 10.0, 25.0),
        "note": "PE 与增速匹配 + 扣非与现金流验真",
    },
    "cyclical": {
        "hard": [
            ("pb", "<=", 3.5),
            ("div_yield", ">=", 2.0),
            ("pe_annualized", "between", (4.0, 25.0)),
        ],
        "band": ("pb", 0.6, 3.5),
        "note": "周期看位置：PB 低位 + 股息托底；中枢利润口径需三年均值（二期）",
    },
    "growth": {
        "hard": [
            ("pb", "<=", 10.0),
            ("revenue_yoy", ">=", 12.0),
        ],
        "band": ("pb", 1.0, 10.0),
        "note": "成长股主要风险在 PB 与空间；无利润时看 PS（ps_to_growth），有利润才看 PE",
    },
    "insurance": {
        "hard": [
            ("pb", "between", (0.5, 1.5)),
            ("div_yield", ">=", 3.5),
            ("roe", ">=", 7.0),
        ],
        "band": ("pb", 0.5, 1.5),
        "note": "degraded：营运利润/NBV 无数据线，以 PB+股息率+ROE 近似，结论仅供参考",
        "degraded": True,
    },
    "property": {
        "hard": [],
        "band": ("pb", 0.3, 2.0),
        "note": "地产只做投机：去化与减值数据不足，不参与评分，仅列观察",
        "observe_only": True,
    },
    "other": {
        "hard": [],
        "band": ("pe_annualized", 10.0, 30.0),
        "note": "未映射行业不参评",
        "observe_only": True,
    },
}

# 评分权重（合计 100）
WEIGHTS = {"valuation": 40, "quality": 30, "cashflow": 15, "growth": 15}

# 估值类门槛属「关键门槛」：不满足即为「贵/不在区间」，直接排除而不是观察
# （对应「区间越界才动手」「赔率不够就不做」；否则一只 60 倍 PE 的消费股也会被列为观察）
CRITICAL_FACTORS = frozenset({"pe_annualized", "pe_static", "pb", "div_yield"})

# 财报可信度 gate 扣分项
GATE_RULES = [
    ("ocf_to_profit", 0.6, -25.0, "净现比过低：利润没有变成钱"),
    ("ocf_to_profit", 0.8, -12.0, "净现比偏低：需核对应收与存货"),
    ("deduct_ratio", 70.0, -18.0, "扣非占比低：靠非经常性损益"),
    ("deduct_ratio", 85.0, -8.0, "扣非占比偏低"),
    ("debt_to_asset", 75.0, -12.0, "资产负债率过高"),
    ("quick_ratio", 0.8, -8.0, "速动比率不足 0.8"),
]
GATE_REJECT = 60.0
GATE_WATCH = 75.0


# ---------- 基础工具 ----------
def _num(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def style_of(industry: str) -> str:
    """行业名 → 风格（关键词包含匹配；未命中归 other）。"""
    ind = industry or ""
    for style, kws in STYLE_MAP:
        if any(k in ind for k in kws):
            return style
    return "other"


def _cmp(value: float | None, op: str, target) -> bool | None:
    """True/False；value 缺失返回 None（调用方按 skip 处理）。"""
    if value is None:
        return None
    if op == "between":
        lo, hi = target
        return lo <= value <= hi
    if op == ">=":
        return value >= target
    if op == "<=":
        return value <= target
    if op == ">":
        return value > target
    if op == "<":
        return value < target
    return None


def _position(value: float | None, lo: float, hi: float) -> float | None:
    """区间位置（0=下沿，1=上沿；越界裁到 0/1）。"""
    v = _num(value)
    if v is None or hi <= lo:
        return None
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


# ---------- 第 0 层：财报可信度 ----------
# 行业豁免：金融与地产的高负债/低速动是经营模式，不能按制造业阈值扣分（否则误杀全部银行）
GATE_EXEMPT = {
    "bank": {"debt_to_asset", "quick_ratio"},
    "insurance": {"debt_to_asset", "quick_ratio"},
    "property": {"quick_ratio"},
}


def credibility(metrics: dict, style: str = "other") -> dict:
    """财报可信度评分（0~100）+ 红旗清单。仅用库内已有指标，不做造假定性。"""
    exempt = GATE_EXEMPT.get(style, set())
    score = 100.0
    reds: list[str] = []
    for factor, threshold, penalty, desc in GATE_RULES:
        if factor in exempt:
            continue
        v = _num(metrics.get(factor))
        if v is None:
            continue
        if factor in ("ocf_to_profit",):
            hit = v < threshold
        else:
            # 扣非比、负债率、速动比率：阈值以下扣分（速动比率同样越低越差）
            hit = v < threshold
        if hit:
            score += penalty
            reds.append(f"{desc}（{factor}={v:.2f}）")

    # 利润与现金流背离：利润为正增长而经营现金流为负增长
    py, oy = _num(metrics.get("profit_yoy")), _num(metrics.get("ocf_yoy"))
    if py is not None and oy is not None and py > 0 and oy < 0:
        score -= 15.0
        reds.append(f"利润与现金流背离（profit_yoy={py:.1f}%，ocf_yoy={oy:.1f}%）")

    score = max(0.0, min(100.0, score))
    verdict = "reject" if score < GATE_REJECT else ("watch" if score < GATE_WATCH else "pass")
    return {"score": round(score, 1), "verdict": verdict, "red_flags": reds}


# ---------- 第 2 层：分项评分 ----------
def _score_valuation(style: str, v: dict) -> float:
    band = RULES.get(style, RULES["other"])["band"]
    factor, lo, hi = band
    pos = _position(v.get(factor), lo, hi)
    if pos is None:
        # 兜底：用 pb 或 pe 任一可用
        for alt in ("pe_annualized", "pb", "ps"):
            if v.get(alt) is not None:
                pos = 0.5
                break
        else:
            return 0.0
    return WEIGHTS["valuation"] * (1.0 - pos)


def _score_quality(v: dict) -> float:
    roe = _num(v.get("roe"))
    deduct = _num(v.get("deduct_ratio"))
    s = 0.0
    if roe is not None:
        s += min(1.0, max(0.0, roe / 20.0)) * (WEIGHTS["quality"] * 0.7)
    if deduct is not None:
        s += min(1.0, max(0.0, (deduct - 70.0) / 30.0)) * (WEIGHTS["quality"] * 0.3)
    return s


def _score_cashflow(v: dict) -> float:
    ocf = _num(v.get("ocf_to_profit"))
    if ocf is None:
        return 0.0
    return min(1.0, max(0.0, ocf / 1.2)) * WEIGHTS["cashflow"]


def _score_growth(v: dict) -> float:
    g = _num(v.get("revenue_yoy"))
    if g is None:
        return 0.0
    return min(1.0, max(0.0, g / 30.0)) * WEIGHTS["growth"]


# ---------- 主评估 ----------
def evaluate_one(code: str, name: str, industry: str, v: dict) -> dict:
    """单票评估：返回档位、分数、逐条命中、区间位置与理由。"""
    style = style_of(industry)
    rule = RULES.get(style, RULES["other"])
    gate = credibility(v, style)
    checks: list[dict] = []
    hard_all_pass = True
    hard_total = hard_pass = 0

    for factor, op, target in rule["hard"]:
        val = _num(v.get(factor))
        ok = _cmp(val, op, target)
        hard_total += 1
        if ok is None:
            checks.append({"factor": factor, "op": op, "target": target, "value": None,
                           "pass": None, "skipped": True,
                           "reason": "数据缺失，门槛跳过（不视为通过）"})
            continue
        if ok:
            hard_pass += 1
        else:
            hard_all_pass = False
        checks.append({"factor": factor, "op": op, "target": target,
                       "value": round(val, 4), "pass": bool(ok), "skipped": False})

    # 现金流背离单独列为提示项（不参与门槛，但影响可信度）
    py, oy = _num(v.get("profit_yoy")), _num(v.get("ocf_yoy"))
    if py is not None and oy is not None and py > 0 and oy < 0:
        checks.append({"factor": "ocf_yoy", "op": "signal", "target": ">0",
                       "value": round(oy, 2), "pass": False, "skipped": False,
                       "reason": "利润正增长但现金流负增长（背离）"})

    score = (_score_valuation(style, v) + _score_quality(v)
             + _score_cashflow(v) + _score_growth(v))

    factor_band, lo, hi = rule["band"]
    pos = _position(v.get(factor_band), lo, hi)

    # 关键估值门槛不满足 → 直接排除（"贵了就是贵了"，不给观察档）
    critical_fail = [c for c in checks
                     if c.get("pass") is False and c.get("factor") in CRITICAL_FACTORS]

    # 档位判定：gate 优先 → 关键门槛 → 硬门槛 → 分数
    if rule.get("observe_only"):
        verdict = "watch"
        decision = "观察：该风格不参与评分（" + rule["note"] + "）"
    elif gate["verdict"] == "reject":
        verdict = "reject"
        decision = "排除：财报可信度不足（" + "；".join(gate["red_flags"][:2]) + "）"
    elif critical_fail:
        f = critical_fail[0]
        verdict = "reject"
        decision = "排除：估值不在区间（%s %s %s，实际 %s）" % (
            f["factor"], f["op"], f["target"], f["value"])
    elif hard_all_pass:
        verdict = "pass" if score >= 65 else "watch"
        decision = "通过：" + ("全部门槛满足" if verdict == "pass" else "门槛满足但综合分偏低")
    elif hard_pass >= max(1, hard_total - 1) and score >= 45:
        verdict = "watch"
        decision = "观察：仅少数门槛未达（%d/%d）" % (hard_pass, hard_total)
    else:
        verdict = "reject"
        decision = "排除：硬门槛未达（%d/%d）" % (hard_pass, hard_total)

    reasons: list[str] = []
    if pos is not None:
        reasons.append(f"估值位于区间 {pos * 100:.0f}%（{factor_band} {lo:g}~{hi:g}）")
    if v.get("div_yield") is not None:
        reasons.append(f"股息率 {v['div_yield']:.2f}%")
    if v.get("pe_annualized") is not None:
        reasons.append(f"PE(年化) {v['pe_annualized']:.1f}")
    if gate["red_flags"]:
        reasons.append("财报红旗：" + "；".join(gate["red_flags"][:2]))
    if any(c.get("skipped") for c in checks):
        reasons.append("部分门槛因数据缺失跳过")

    return {
        "code": code, "name": name, "industry": industry, "style": style,
        "style_label": STYLE_LABEL.get(style, style),
        "score": round(score, 1), "verdict": verdict, "decision": decision,
        "credibility": gate["score"], "credibility_verdict": gate["verdict"],
        "red_flags": gate["red_flags"],
        "checks": checks,
        "position": round(pos, 4) if pos is not None else None,
        "band": {"factor": factor_band, "low": lo, "high": hi},
        "reasons": reasons,
        "valuation": {k: (round(x, 4) if isinstance(x, float) else x)
                      for k, x in v.items() if k != "report_date"},
        "report_date": v.get("report_date"),
        "rules_note": rule["note"],
    }


def scan(db: Session, trade_date: date | None = None, style: str | None = None,
         verdict: str | None = None, limit: int = 200) -> dict:
    """全市场扫描：构建估值快照 → 逐票评估 → 过滤/排序。"""
    snap = build_snapshot(db, trade_date=trade_date)
    rows = snap["rows"]
    info = industry_map(db, list(rows.keys()))

    results: list[dict] = []
    for code, v in rows.items():
        name, ind = info.get(code, ("", ""))
        results.append(evaluate_one(code, name, ind, v))

    stats = {"total": len(results), "pass": 0, "watch": 0, "reject": 0}
    for r in results:
        stats[r["verdict"]] = stats.get(r["verdict"], 0) + 1

    picked = results
    if style:
        picked = [r for r in picked if r["style"] == style]
    if verdict:
        picked = [r for r in picked if r["verdict"] == verdict]
    picked.sort(key=lambda r: (-r["score"], r["code"]))

    return {
        "model": MODEL_VERSION,
        "meta": {**snap["meta"], "stats": stats, "filtered": len(picked),
                 "risk_free": DEFAULT_RISK_FREE, "div_premium": DIV_PREMIUM},
        "items": picked[:limit],
        "notes": [
            "演示口径：PE 默认用「最新报告期年化」，与静态 PE 不可混用",
            "股息率依赖分红数据，缺失时对应门槛跳过并在 checks 中标注 skipped",
            "保险依赖 EV/NBV，当前以 PB+股息率+ROE 近似（degraded）",
            "结论为研究辅助，不构成投资建议",
        ],
    }


def evaluate(db: Session, code: str, trade_date: date | None = None) -> dict:
    snap = build_snapshot(db, codes=[code], trade_date=trade_date)
    v = snap["rows"].get(code)
    if not v:
        return {"code": code, "error": "该标的无估值数据（缺价格或财务指标）"}
    name, ind = industry_map(db, [code]).get(code, ("", ""))
    out = evaluate_one(code, name, ind, v)
    out["meta"] = snap["meta"]
    return out


def rules_meta() -> dict:
    """规则元数据（前端规则配置页用）。"""
    return {
        "model": MODEL_VERSION,
        "weights": WEIGHTS,
        "risk_free": DEFAULT_RISK_FREE,
        "div_premium": DIV_PREMIUM,
        "gate": {"reject_below": GATE_REJECT, "watch_below": GATE_WATCH},
        "styles": [
            {"style": s, "label": STYLE_LABEL.get(s, s), **RULES[s]}
            for s in RULES
        ],
    }