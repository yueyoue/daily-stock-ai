"""盘前早报模板（T7.2，开发文档 §3.2 七维度口径 / §4 模块3 报告引擎）。

本模块只负责「盘前早报」的模板层，T7.4 负责把它接进报告流水线（入库/版本/重试），
T7.5 负责定时触发。

- collect_morning_input：结构化输入——隔夜资讯、上一交易日大盘/资金/情绪、热点题材、
  事件日历（当前无结构化事件源，显式标注缺口，不允许 LLM 编造）
- MORNING_SYSTEM_PROMPT + build_morning_prompt：固定 Prompt（7 维度硬约束 + 必填字段清单）
- validate_sections：7 维度覆盖校验，缺一即不合格
- verify_key_numbers：关键数值与结构化输入比对，字段缺失 / 数值不一致均拦截
- generate_morning：结构化输入 -> LLM -> Markdown -> 校验；不合格则带问题清单重试一次
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from app.models.content import HotTopic, News
from app.models.market import MarketDaily
from app.services.pipeline import log_stage
from app.services.trading_calendar import previous_trading_day

TASK_NAME = "盘前早报"

# 7 维度（开发文档 §3.2）：(规范名, 正文识别标记)
MORNING_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("速读摘要", ("速读摘要",)),
    ("隔夜市场大事", ("隔夜市场大事", "隔夜大事", "隔夜市场")),
    ("市场情绪预判", ("市场情绪预判", "情绪预判")),
    ("资金动向", ("资金动向",)),
    ("操作提醒/风险提示", ("操作提醒", "风险提示")),
    ("今日观察方向", ("今日观察方向", "观察方向")),
    ("今日重要事件与数据日历", ("数据日历", "事件日历", "重要事件")),
)
REQUIRED_DIMENSIONS: tuple[str, ...] = tuple(name for name, _ in MORNING_SECTIONS)

NEWS_LIMIT = 12          # 隔夜资讯条数上限
CONTENT_SNIPPET = 120    # 单条资讯正文截断
HOT_LIMIT = 8            # 热点题材条数上限
CHARS_TARGET = 2000      # 目标篇幅（约 2000 字，仅作提示不作为硬门槛）

MORNING_SYSTEM_PROMPT = (
    "你是 A 股盘前投研分析师。只依据给定的结构化数据撰写盘前早报，不得编造未提供的数据；"
    "数据缺失的维度如实写明「暂无结构化数据」。输出 Markdown，必须且只能包含下列 7 个二级标题，"
    "标题文字与序号不得改动或省略：\n"
    "## 1. 速读摘要\n"
    "## 2. 隔夜市场大事\n"
    "## 3. 市场情绪预判\n"
    "## 4. 资金动向\n"
    "## 5. 操作提醒 / 风险提示\n"
    "## 6. 今日观察方向\n"
    "## 7. 今日重要事件与数据日历\n"
    "全文约 2000 字。结构化输入中的「必填字段」必须在对应维度正文里逐字写出字段名与数值。"
    "北向资金只有成交额口径（净流入为季度披露，不可实时取），不得给出北向净流入数值。"
)

# 数值校验时允许的同义字段名
LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "涨停家数": ("涨停家数", "涨停数量"),
    "跌停家数": ("跌停家数", "跌停数量"),
    "上涨家数": ("上涨家数",),
    "下跌家数": ("下跌家数",),
    "市场情绪得分": ("市场情绪得分", "情绪得分"),
    "主力资金净流入": ("主力资金净流入", "主力净流入"),
}


# ---------- 结构化输入 ----------

def _fmt_number(value: float, digits: int) -> str:
    if digits == 0:
        return str(int(round(value)))
    return f"{value:.{digits}f}"


def _key_numbers(market: MarketDaily | None) -> list[dict]:
    """关键数值清单（用于 Prompt 必填字段 + 生成后校验）。金额单位统一为亿元。"""
    if market is None:
        return []
    specs: list[tuple[str, float | None, str, int]] = [
        ("涨停家数", market.limit_up_count, "家", 0),
        ("跌停家数", market.limit_down_count, "家", 0),
        ("上涨家数", market.up_count, "家", 0),
        ("下跌家数", market.down_count, "家", 0),
        ("市场情绪得分", market.sentiment_score, "分", 1),
    ]
    if market.main_moneyflow is not None:
        specs.append(("主力资金净流入", market.main_moneyflow / 1e8, "亿元", 2))
    out: list[dict] = []
    for label, value, unit, digits in specs:
        if value is None:
            continue
        num = float(value)
        out.append({"label": label, "value": num, "unit": unit,
                    "display": _fmt_number(num, digits)})
    return out


def _overnight_news(db: Session, d: date, limit: int = NEWS_LIMIT) -> list[dict]:
    """隔夜资讯：前一日 06:00 至当日 08:00 窗口，优先高/中影响等级。"""
    start = datetime.combine(d, time.min) - timedelta(hours=18)
    end = datetime.combine(d, time(8, 0))
    base = (db.query(News)
            .filter(News.published_at.is_not(None))
            .filter(News.published_at >= start)
            .filter(News.published_at <= end))
    rows = (base.filter(News.impact_level.in_(("高", "中")))
            .order_by(News.published_at.desc()).limit(limit).all())
    if not rows:
        rows = base.order_by(News.published_at.desc()).limit(limit).all()
    return [
        {"title": r.title, "source": r.source,
         "impact_level": r.impact_level or "未分级",
         "published_at": r.published_at.isoformat() if r.published_at else None,
         "summary": (r.content or "")[:CONTENT_SNIPPET]}
        for r in rows
    ]


def _hot_topics(db: Session, prev: date, limit: int = HOT_LIMIT) -> list[dict]:
    """最近一期（不晚于上一交易日）的热点题材，高热度优先。"""
    latest = (db.query(HotTopic.date)
              .filter(HotTopic.date <= prev)
              .order_by(HotTopic.date.desc()).first())
    if latest is None:
        return []
    heat_order = {"高": 0, "中": 1, "低": 2}
    rows = db.query(HotTopic).filter(HotTopic.date == latest[0]).all()
    rows.sort(key=lambda r: heat_order.get(r.heat, 3))
    return [
        {"date": r.date.isoformat(), "title": r.title, "heat": r.heat,
         "industry_chain": r.industry_chain,
         "related_codes": list(r.related_codes or [])}
        for r in rows[:limit]
    ]


NO_EVENT_SOURCE_NOTE = "暂无结构化事件源（新股申购/解禁/经济数据/财报日程待接入），不得编造具体事件"


def _event_calendar(db: Session, d: date) -> dict:
    """事件日历：当前无结构化事件表，显式返回缺口，避免 LLM 编造。"""
    return {"date": d.isoformat(), "items": [], "note": NO_EVENT_SOURCE_NOTE}


def _market_payload(market: MarketDaily | None, prev: date) -> dict:
    if market is None:
        return {"trade_date": prev.isoformat(), "available": False}
    extra = market.extra or {}
    return {
        "trade_date": market.date.isoformat(),
        "available": True,
        "prev_close_is_latest": market.date != prev,
        "up_count": market.up_count,
        "down_count": market.down_count,
        "limit_up_count": market.limit_up_count,
        "limit_down_count": market.limit_down_count,
        "sentiment_score": market.sentiment_score,
        "main_moneyflow": market.main_moneyflow,
        "max_consecutive": extra.get("最高连板"),
        "consecutive_ladder": extra.get("连板梯队"),
        "main_sectors": extra.get("main_sectors"),
    }


def collect_morning_input(db: Session, d: date) -> dict:
    """汇总盘前早报的结构化输入。"""
    prev = previous_trading_day(db, d)
    market = (db.query(MarketDaily).filter(MarketDaily.date <= prev)
              .order_by(MarketDaily.date.desc()).first())
    return {
        "trade_date": d.isoformat(),
        "prev_trade_date": prev.isoformat(),
        "market": _market_payload(market, prev),
        "key_numbers": _key_numbers(market),
        "overnight_news": _overnight_news(db, d),
        "hot_topics": _hot_topics(db, prev),
        "events": _event_calendar(db, d),
    }


# ---------- Prompt ----------

def build_morning_prompt(payload: dict, corrections: list[str] | None = None) -> str:
    """固定 Prompt：结构化输入 -> 文本；corrections 为重试时回灌的问题清单。"""
    lines: list[str] = []
    lines.append(f"报告日期：{payload['trade_date']}；基准交易日：{payload['prev_trade_date']}")
    lines.append("")
    lines.append("【必填字段】下列字段名与数值必须逐字出现在正文对应维度中：")
    if payload["key_numbers"]:
        for spec in payload["key_numbers"]:
            lines.append(f"- {spec['label']}：{spec['display']}{spec['unit']}")
    else:
        lines.append("- （上一交易日大盘数据缺失，请如实说明暂无数据）")
    lines.append("")

    market = payload["market"]
    lines.append("【上一交易日大盘/资金/情绪】")
    if market.get("available"):
        lines.append(f"- 统计日：{market['trade_date']}（{'非上一交易日，为最近可用' if market.get('prev_close_is_latest') else '即上一交易日'}）")
        for key, name in (("up_count", "上涨家数"), ("down_count", "下跌家数"),
                          ("limit_up_count", "涨停家数"), ("limit_down_count", "跌停家数"),
                          ("sentiment_score", "市场情绪得分")):
            if market.get(key) is not None:
                lines.append(f"- {name}：{market[key]}")
        if market.get("main_moneyflow") is not None:
            lines.append(f"- 主力资金净流入：{market['main_moneyflow'] / 1e8:.2f}亿元（东财口径）")
        if market.get("max_consecutive") is not None:
            lines.append(f"- 最高连板：{market['max_consecutive']} 板")
        if market.get("consecutive_ladder"):
            ladder = "，".join(f"{k} {v} 家" for k, v in market["consecutive_ladder"].items())
            lines.append(f"- 连板梯队：{ladder}")
        if market.get("main_sectors"):
            sectors = "，".join(
                f"{s.get('name')}（净流入 {s.get('moneyflow', 0) / 1e8:.2f}亿元）"
                if isinstance(s, dict) else str(s) for s in market["main_sectors"])
            lines.append(f"- 主力净流入板块：{sectors}")
    else:
        lines.append("- 暂无数据（大盘汇总尚未采集）")
    lines.append("")

    lines.append("【隔夜资讯（已按影响等级降噪）】")
    if payload["overnight_news"]:
        for item in payload["overnight_news"]:
            lines.append(f"- [{item['impact_level']}] {item['title']}"
                         f"（{item['source']}，{item['published_at']}）{item['summary']}")
    else:
        lines.append("- 暂无隔夜资讯")
    lines.append("")

    lines.append("【最近热点题材】")
    if payload["hot_topics"]:
        for topic in payload["hot_topics"]:
            codes = "、".join(topic["related_codes"]) or "—"
            lines.append(f"- {topic['title']}（热度 {topic['heat']}；产业链 {topic['industry_chain'] or '—'}；"
                         f"关联个股 {codes}）")
    else:
        lines.append("- 暂无热点归档")
    lines.append("")

    lines.append("【事件日历】")
    lines.append(f"- {payload['events']['note']}")
    lines.append("")

    if corrections:
        lines.append("【上一次输出不合格，必须逐条修正】")
        for problem in corrections:
            lines.append(f"- {problem}")
        lines.append("请重新输出完整 7 维度 Markdown，不要只输出修改部分。")
    return "\n".join(lines)


# ---------- 校验 ----------

def validate_sections(md: str) -> list[str]:
    """返回缺失的维度规范名（空列表 = 7 维度齐全）。"""
    text = md or ""
    missing: list[str] = []
    for name, markers in MORNING_SECTIONS:
        if not any(marker in text for marker in markers):
            missing.append(name)
    return missing


def _find_number(md: str, label: str) -> float | None:
    names = LABEL_ALIASES.get(label, (label,))
    for name in names:
        m = re.search(re.escape(name) + r"[^\d\-+]{0,12}?(-?\d+(?:\.\d+)?)", md)
        if m:
            return float(m.group(1))
    return None


def verify_key_numbers(md: str, key_numbers: list[dict],
                       tol_ratio: float = 0.02, tol_abs: float = 0.01) -> list[str]:
    """关键数值校验：字段缺失 / 数值与结构化输入不一致均返回问题描述。"""
    problems: list[str] = []
    for spec in key_numbers:
        label = spec["label"]
        want = float(spec["value"])
        got = _find_number(md or "", label)
        if got is None:
            problems.append(f"缺少字段：「{label}」未在正文出现"
                            f"（结构化输入={spec['display']}{spec['unit']}）")
            continue
        tol = max(tol_abs, abs(want) * tol_ratio)
        if abs(got - want) > tol:
            problems.append(f"数值不一致：「{label}」正文={got:g}，"
                            f"结构化输入={spec['display']}{spec['unit']}")
    return problems


# ---------- 生成 ----------

def generate_morning(db: Session, d: date, *, llm=None, max_attempts: int = 2,
                     max_tokens: int = 3000, temperature: float = 0.3) -> dict:
    """生成盘前早报正文（不写库；入库/版本由 T7.4 流水线负责）。

    返回 {ok, report_date, attempts, problems, missing_sections, number_problems,
          md, chars, input}；ok=False 表示重试若干次后仍存在维度缺失或数值问题。
    """
    if llm is None:
        from app.services.llm import complete as llm  # 延迟导入：无 litellm 环境也可 import 本模块

    payload = collect_morning_input(db, d)
    prompt = build_morning_prompt(payload)
    md = ""
    attempts = 0
    problems: list[str] = []
    missing: list[str] = []
    number_problems: list[str] = []

    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        if attempt > 1:
            prompt = build_morning_prompt(payload, corrections=problems)
        md = llm(db, prompt, system=MORNING_SYSTEM_PROMPT,
                 max_tokens=max_tokens, temperature=temperature)
        missing = validate_sections(md)
        number_problems = verify_key_numbers(md, payload["key_numbers"])
        problems = [f"缺少维度：{name}" for name in missing] + number_problems
        if not problems:
            break

    ok = not problems
    log_stage(db, TASK_NAME, "success" if ok else "failed",
              message=(f"date={d.isoformat()} attempts={attempts} chars={len(md)} "
                       f"problems={'；'.join(problems) if problems else '无'}"))
    return {
        "ok": ok, "report_date": d.isoformat(), "attempts": attempts,
        "problems": problems, "missing_sections": missing,
        "number_problems": number_problems, "md": md, "chars": len(md),
        "input": payload,
    }