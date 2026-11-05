"""盘后复盘模板（T7.3，开发文档 §3.2 / §4 模块3「盘后复盘 17:00」七要素口径）。

本模块只负责「盘后复盘」的模板层，T7.4 负责把它接进报告流水线（入库/版本/重试），
T7.5 负责定时触发。

- 市场情绪打分：规则化打分（`app/services/sentiment.py`，规则文档 `sentiment_rules.md`），
  可人工复算；本模块在生成前把得分写回 `market_daily.sentiment_score`
- 涨停数量与涨停梯队：涨停/跌停家数 + 最高连板 + 连板梯队
- 主线板块：`market_daily.extra.main_sectors`，缺省回退当日热点归档
- 资金流向：主力资金净流入（东财口径；北向仅成交额口径，不给净流入）
- 风险预警：规则化清单（跌停家数 / 市场广度 / 情绪分 / 资金流出阈值）
- 次日观察计划：次日交易日 + 当日主线板块 + 当日选股信号
- 策略成功率自动验证：当日已保存策略的选股信号 -> 次日行情涨跌对账

校验复用盘前早报的 `validate_sections` / `verify_key_numbers`（标签口径一致），
不合格则带问题清单重试一次；不写 Report 表（入库/版本由 T7.4 流水线负责）。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from app.models.content import HotTopic
from app.models.market import MarketDaily, StockKline
from app.models.system import StockStrategy
from app.services.pipeline import log_stage
from app.services.report_morning import (
    LABEL_ALIASES,
    _find_number,
    _fmt_number,
    verify_key_numbers,
)
from app.services.sentiment import apply_sentiment_score, score_of_market
from app.services.trading_calendar import next_trading_day

TASK_NAME = "盘后复盘"

# 7 要素（开发文档 §4 模块3）：(规范名, 正文识别标记)
AFTERNOON_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("市场情绪打分", ("市场情绪打分", "情绪打分")),
    ("涨停数量与涨停梯队", ("涨停数量", "涨停梯队")),
    ("主线板块", ("主线板块",)),
    ("资金流向", ("资金流向",)),
    ("风险预警", ("风险预警",)),
    ("次日观察计划", ("次日观察计划", "次日计划", "观察计划")),
    ("策略成功率验证", ("策略成功率", "成功率验证", "策略验证")),
)
REQUIRED_DIMENSIONS: tuple[str, ...] = tuple(name for name, _ in AFTERNOON_SECTIONS)

HOT_LIMIT = 8        # 回退热点条数上限
CHARS_TARGET = 2000  # 目标篇幅（约 2000 字，仅作提示不作硬门槛）

# 风险预警规则阈值（规则化清单，保证可复现）
RISK_LIMIT_DOWN = 20       # 跌停家数阈值（家）
RISK_SENTIMENT_LOW = 40.0  # 情绪分偏低阈值
RISK_MONEYFLOW = 100e8     # 主力资金净流出阈值（元）

AFTERNOON_SYSTEM_PROMPT = (
    "你是 A 股盘后复盘分析师。只依据给定的结构化数据撰写盘后复盘，不得编造未提供的数据；"
    "数据缺失的要素如实写明「暂无结构化数据」。输出 Markdown，必须且只能包含下列 7 个二级标题，"
    "标题文字与序号不得改动或省略：\n"
    "## 1. 市场情绪打分\n"
    "## 2. 涨停数量与涨停梯队\n"
    "## 3. 主线板块\n"
    "## 4. 资金流向\n"
    "## 5. 风险预警\n"
    "## 6. 次日观察计划\n"
    "## 7. 策略成功率验证\n"
    "全文约 2000 字。结构化输入中的「必填字段」必须在对应要素正文里逐字写出字段名与数值。"
    "市场情绪打分必须直接采用输入给出的规则化得分与定性，不得自行调整口径。"
    "风险预警只依据输入给出的规则化清单展开，不得新增未触发的风险。"
    "策略成功率验证必须写明信号数、有效样本数、成功数与成功率（无信号时如实说明）。"
    "北向资金只有成交额口径（净流入为季度披露，不可实时取），不得给出北向净流入数值。"
)


# ---------- 结构化输入 ----------

def _key_numbers(market: MarketDaily | None, score: float | None) -> list[dict]:
    """关键数值清单（用于 Prompt 必填字段 + 生成后校验）。金额单位统一为亿元。"""
    specs: list[tuple[str, float | None, str, int]] = []
    if market is not None:
        specs += [
            ("涨停家数", market.limit_up_count, "家", 0),
            ("跌停家数", market.limit_down_count, "家", 0),
            ("上涨家数", market.up_count, "家", 0),
            ("下跌家数", market.down_count, "家", 0),
        ]
    specs.append(("市场情绪得分", score, "分", 1))
    if market is not None and market.main_moneyflow is not None:
        specs.append(("主力资金净流入", market.main_moneyflow / 1e8, "亿元", 2))

    out: list[dict] = []
    for label, value, unit, digits in specs:
        if value is None:
            continue
        num = float(value)
        out.append({"label": label, "value": num, "unit": unit,
                    "display": _fmt_number(num, digits)})
    return out


def _ladder(market: MarketDaily | None) -> dict:
    extra = (market.extra or {}) if market is not None else {}
    return {"max_consecutive": extra.get("最高连板"),
            "ladder": extra.get("连板梯队")}


def _main_sectors(db: Session, d: date, market: MarketDaily | None) -> list[dict]:
    """主线板块：优先 market_daily.extra.main_sectors，缺省回退当日热点归档。"""
    extra = (market.extra or {}) if market is not None else {}
    raw = extra.get("main_sectors")
    if raw:
        out: list[dict] = []
        for s in raw:
            if isinstance(s, dict):
                out.append({"name": s.get("name"), "moneyflow": s.get("moneyflow"),
                            "source": "market_daily"})
            else:
                out.append({"name": str(s), "moneyflow": None, "source": "market_daily"})
        return out

    heat_order = {"高": 0, "中": 1, "低": 2}
    rows = db.query(HotTopic).filter(HotTopic.date == d).all()
    rows.sort(key=lambda r: heat_order.get(r.heat, 3))
    return [{"name": r.title, "moneyflow": None, "source": "hot_topic",
             "heat": r.heat, "industry_chain": r.industry_chain,
             "related_codes": list(r.related_codes or [])}
            for r in rows[:HOT_LIMIT]]


def _moneyflow_payload(market: MarketDaily | None) -> dict:
    if market is None or market.main_moneyflow is None:
        return {"available": False}
    yi = market.main_moneyflow / 1e8
    return {"available": True, "net_inflow_yi": round(yi, 2),
            "direction": "净流入" if yi >= 0 else "净流出", "scope": "东财口径"}


def _risk_warnings(market: MarketDaily | None, score: float | None) -> list[str]:
    """规则化风险预警清单（阈值见模块常量，规则可复现）。"""
    if market is None:
        return ["当日大盘汇总缺失（涨跌家数/涨跌停/资金均为空），无法生成规则化风险预警，请核实采集线"]

    risks: list[str] = []
    if market.limit_down_count is not None and market.limit_down_count >= RISK_LIMIT_DOWN:
        risks.append(f"跌停家数 {market.limit_down_count} 家（不低于 {RISK_LIMIT_DOWN} 家），警惕恐慌盘扩散")
    if (market.up_count is not None and market.down_count is not None
            and market.down_count > market.up_count):
        risks.append(f"下跌家数多于上涨家数（{market.down_count} 家 > {market.up_count} 家），市场广度偏弱")
    if score is not None and score < RISK_SENTIMENT_LOW:
        risks.append(f"市场情绪得分 {score}（低于 {RISK_SENTIMENT_LOW:g} 分），情绪偏低，注意控制仓位")
    if (market.main_moneyflow is not None and market.main_moneyflow < 0
            and abs(market.main_moneyflow) >= RISK_MONEYFLOW):
        risks.append(f"主力资金净流出 {abs(market.main_moneyflow) / 1e8:.2f} 亿元"
                     f"（不低于 {RISK_MONEYFLOW / 1e8:g} 亿元），资金面承压")
    if not risks:
        risks.append("未触发规则化风险预警（跌停家数、市场广度、情绪分、资金净流出均未达阈值）")
    return risks


def _default_signal_fn(db: Session, strategy: StockStrategy, day: date) -> dict:
    """默认信号函数：对已保存策略执行确定性全市场扫描（T5.2）。"""
    from app.services.selector import run_scan  # 延迟导入：便于测试 monkeypatch / 注入

    conditions = (strategy.conditions or {}).get("conditions", []) if strategy is not None else []
    return run_scan(db, conditions or [], day)


def verify_strategy_success(
    db: Session,
    d: date,
    *,
    hold_days: int = 1,
    strategies: list | None = None,
    signal_fn=None,
) -> dict:
    """策略成功率自动验证：当日信号 -> 第 hold_days 个交易日行情对账。

    信号 = 已保存 `stock_strategy` 各自的全市场扫描候选（默认走 selector.run_scan，
    可通过 signal_fn 注入以便测试）；成功 = 次日 (收盘-开盘)/开盘 > 0。
    同一交易日开收均为不复权价，比例不受复权因子影响。
    """
    if strategies is None:
        strategies = db.query(StockStrategy).order_by(StockStrategy.id.asc()).all()
    fn = signal_fn or _default_signal_fn

    verify_day = d
    for _ in range(max(1, int(hold_days))):
        verify_day = next_trading_day(db, verify_day)

    per_strategy: list[dict] = []
    unique: dict[str, dict] = {}
    for st in strategies:
        try:
            res = fn(db, st, d) or {}
        except Exception as e:  # noqa: BLE001 单策略失败不拖垮整体对账
            per_strategy.append({"strategy": getattr(st, "name", None),
                                 "signal_count": 0, "signals": [], "error": str(e)})
            continue
        cands = res.get("candidates") or []
        codes = [c.get("code") for c in cands if c.get("code")]
        per_strategy.append({"strategy": getattr(st, "name", None),
                             "signal_count": len(codes), "signals": codes[:50]})
        for c in cands:
            code = c.get("code")
            if code and code not in unique:
                unique[code] = {"code": code, "name": c.get("name") or "",
                                "strategy": getattr(st, "name", None)}

    klines: dict[str, StockKline] = {}
    if unique:
        rows = (db.query(StockKline)
                .filter(StockKline.date == verify_day, StockKline.code.in_(list(unique)))
                .all())
        klines = {r.code: r for r in rows}

    details: list[dict] = []
    success = valid = 0
    for code, meta in unique.items():
        k = klines.get(code)
        if k is None or not k.open:
            details.append({"code": code, "name": meta["name"], "strategy": meta["strategy"],
                            "return_pct": None, "result": "无次日行情"})
            continue
        r = (k.close - k.open) / k.open
        ok = r > 0
        valid += 1
        success += 1 if ok else 0
        details.append({"code": code, "name": meta["name"], "strategy": meta["strategy"],
                        "open": k.open, "close": k.close,
                        "return_pct": round(r * 100, 2),
                        "result": "成功" if ok else "失败"})

    rate = round(success / valid, 4) if valid else None
    if not per_strategy:
        note = "未保存任何选股策略，无信号可验证"
    elif not unique:
        note = "当日无选股信号，策略成功率不适用"
    else:
        note = ""
    return {
        "trade_date": d.isoformat(),
        "verify_date": verify_day.isoformat(),
        "hold_days": int(hold_days),
        "strategy_count": len(per_strategy),
        "signal_count": len(unique),
        "valid_count": valid,
        "success_count": success,
        "success_rate": rate,
        "has_signal": bool(unique),
        "per_strategy": per_strategy,
        "details": details,
        "note": note,
    }


def _next_day_plan(db: Session, d: date, sectors: list[dict], strategy: dict) -> dict:
    nxt = next_trading_day(db, d)
    watch = [f"{item['name']}({item['code']})" if item.get("name") else item["code"]
             for item in strategy.get("details", [])[:10]]
    note = ("次日为交易日，参考当日主线板块与选股信号设置观察池"
            if nxt else "次日非交易日（日历缺失时按周一~周五兜底）")
    return {"next_trade_date": nxt.isoformat(),
            "sectors": [s["name"] for s in sectors if s.get("name")],
            "watch": watch, "note": note}


def collect_afternoon_input(db: Session, d: date) -> dict:
    """汇总盘后复盘的结构化输入（情绪分按规则复算，不改库）。"""
    market = db.get(MarketDaily, d)
    sentiment = score_of_market(market)
    sectors = _main_sectors(db, d, market)
    strategy = verify_strategy_success(db, d)
    return {
        "trade_date": d.isoformat(),
        "sentiment": sentiment,
        "key_numbers": _key_numbers(market, sentiment["score"]),
        "ladder": _ladder(market),
        "sectors": sectors,
        "moneyflow": _moneyflow_payload(market),
        "risks": _risk_warnings(market, sentiment["score"]),
        "strategy": strategy,
        "next_day": _next_day_plan(db, d, sectors, strategy),
        "market_available": market is not None,
    }


# ---------- Prompt ----------

def build_afternoon_prompt(payload: dict, corrections: list[str] | None = None) -> str:
    """固定 Prompt：结构化输入 -> 文本；corrections 为重试时回灌的问题清单。"""
    lines: list[str] = []
    lines.append(f"复盘日期：{payload['trade_date']}")
    lines.append("")
    lines.append("【必填字段】下列字段名与数值必须逐字出现在正文对应要素中：")
    if payload["key_numbers"]:
        for spec in payload["key_numbers"]:
            lines.append(f"- {spec['label']}：{spec['display']}{spec['unit']}")
    else:
        lines.append("- （当日大盘数据缺失，请如实说明暂无数据）")
    lines.append("")

    sentiment = payload["sentiment"]
    lines.append("【市场情绪打分（规则化，直接用此结果）】")
    if sentiment["score"] is None:
        lines.append("- 无法打分（涨跌家数/涨跌停/资金全部缺失）")
    else:
        lines.append(f"- 市场情绪得分：{sentiment['score']} 分（{sentiment['level']}）；"
                     f"规则版本 {sentiment['version']}")
        for name, part in sentiment["parts"].items():
            lines.append(f"- {name}：{part['score']} / {part['full']:g} 分（{part['note']}）")
        if sentiment["missing"]:
            lines.append(f"- 缺失分项：{'、'.join(sentiment['missing'])}（总分按已具备分项折算百分制）")
    lines.append("")

    lines.append("【涨停数量与涨停梯队】")
    ladder = payload["ladder"]
    if ladder.get("max_consecutive") is not None:
        lines.append(f"- 最高连板：{ladder['max_consecutive']} 板")
    else:
        lines.append("- 最高连板：暂无数据")
    if ladder.get("ladder"):
        txt = "，".join(f"{k} {v} 家" for k, v in ladder["ladder"].items())
        lines.append(f"- 连板梯队：{txt}")
    else:
        lines.append("- 连板梯队：暂无数据")
    lines.append("")

    lines.append("【主线板块】")
    if payload["sectors"]:
        for s in payload["sectors"]:
            mf = (f"，净流入 {s['moneyflow'] / 1e8:.2f} 亿元"
                  if isinstance(s.get("moneyflow"), (int, float)) else "")
            chain = f"；产业链 {s['industry_chain']}" if s.get("industry_chain") else ""
            lines.append(f"- {s['name']}（来源 {s['source']}{mf}{chain}）")
    else:
        lines.append("- 暂无主线板块线索（market_daily.extra 与当日热点归档均为空）")
    lines.append("")

    lines.append("【资金流向】")
    mf = payload["moneyflow"]
    if mf.get("available"):
        lines.append(f"- 主力资金{mf['direction']}：{abs(mf['net_inflow_yi']):.2f} 亿元（{mf['scope']}）")
    else:
        lines.append("- 主力资金净流入：暂无数据")
    lines.append("")

    lines.append("【风险预警（规则化清单，不得新增未触发项）】")
    for risk in payload["risks"]:
        lines.append(f"- {risk}")
    lines.append("")

    lines.append("【策略成功率验证】")
    st = payload["strategy"]
    lines.append(f"- 验证交易日：{st['trade_date']} -> 对账日 {st['verify_date']}（持有 {st['hold_days']} 个交易日）")
    lines.append(f"- 已保存策略数：{st['strategy_count']}；当日信号数：{st['signal_count']}；"
                 f"有效样本：{st['valid_count']}；成功：{st['success_count']}；"
                 f"成功率：{'—' if st['success_rate'] is None else st['success_rate']}")
    if st["note"]:
        lines.append(f"- 说明：{st['note']}")
    for item in st["per_strategy"]:
        lines.append(f"- 策略「{item['strategy']}」信号 {item['signal_count']} 个")
    lines.append("")

    lines.append("【次日观察计划】")
    nd = payload["next_day"]
    lines.append(f"- 次日交易日：{nd['next_trade_date']}")
    lines.append(f"- 参考主线板块：{'、'.join(nd['sectors']) or '—'}")
    lines.append(f"- 当日信号观察池：{'、'.join(nd['watch']) or '—'}")
    lines.append("")

    if corrections:
        lines.append("【上一次输出不合格，必须逐条修正】")
        for problem in corrections:
            lines.append(f"- {problem}")
        lines.append("请重新输出完整 7 要素 Markdown，不要只输出修改部分。")
    return "\n".join(lines)


# ---------- 校验 ----------

def validate_sections(md: str) -> list[str]:
    """返回缺失的要素规范名（空列表 = 7 要素齐全）。"""
    text = md or ""
    missing: list[str] = []
    for name, markers in AFTERNOON_SECTIONS:
        if not any(marker in text for marker in markers):
            missing.append(name)
    return missing


# ---------- 生成 ----------

def generate_afternoon(db: Session, d: date, *, llm=None, max_attempts: int = 2,
                       max_tokens: int = 3000, temperature: float = 0.3) -> dict:
    """生成盘后复盘正文（不写 Report 表；入库/版本由 T7.4 流水线负责）。

    生成前按规则化口径把情绪分写回 `market_daily.sentiment_score`（T7.3 为唯一生产者）。
    返回 {ok, report_date, attempts, problems, missing_sections, number_problems,
          md, chars, sentiment, input}；ok=False 表示重试后仍存在要素缺失或数值问题。
    """
    if llm is None:
        from app.services.llm import complete as llm  # 延迟导入：无 litellm 环境也可 import 本模块

    sentiment = apply_sentiment_score(db, d)
    payload = collect_afternoon_input(db, d)
    prompt = build_afternoon_prompt(payload)
    md = ""
    attempts = 0
    problems: list[str] = []
    missing: list[str] = []
    number_problems: list[str] = []

    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        if attempt > 1:
            prompt = build_afternoon_prompt(payload, corrections=problems)
        md = llm(db, prompt, system=AFTERNOON_SYSTEM_PROMPT,
                 max_tokens=max_tokens, temperature=temperature)
        missing = validate_sections(md)
        number_problems = verify_key_numbers(md, payload["key_numbers"])
        problems = [f"缺少要素：{name}" for name in missing] + number_problems
        if not problems:
            break

    ok = not problems
    log_stage(db, TASK_NAME, "success" if ok else "failed",
              message=(f"date={d.isoformat()} attempts={attempts} chars={len(md)} "
                       f"sentiment={sentiment['score']} "
                       f"problems={'；'.join(problems) if problems else '无'}"))
    return {
        "ok": ok, "report_date": d.isoformat(), "attempts": attempts,
        "problems": problems, "missing_sections": missing,
        "number_problems": number_problems, "md": md, "chars": len(md),
        "sentiment": sentiment, "input": payload,
    }


__all__ = [
    "AFTERNOON_SECTIONS", "AFTERNOON_SYSTEM_PROMPT", "REQUIRED_DIMENSIONS", "TASK_NAME",
    "build_afternoon_prompt", "collect_afternoon_input", "generate_afternoon",
    "validate_sections", "verify_strategy_success",
    "LABEL_ALIASES", "_find_number", "verify_key_numbers",
]