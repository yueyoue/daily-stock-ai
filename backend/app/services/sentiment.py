"""市场情绪规则化打分（T7.3，开发文档 §4 模块3「盘后复盘 · 市场情绪打分」）。

口径固定、可人工复算；规则文档见 `app/services/sentiment_rules.md`（随模板发布）。

三类分项满分合计 100：
- 涨跌广度 40 分 = 40 × 上涨家数 /（上涨家数 + 下跌家数）
- 涨停强度 30 分 = 30 × 涨停家数 /（涨停家数 + 跌停家数）；两者均为 0 时取 15（中性）
- 资金面   30 分 = 按主力资金净流入（亿元）分档取值

缺项处理：某分项原始数据缺失即不参与计算，总分按「已具备分项满分之和」折算百分制，
保证在部分数据缺失时仍给出可比分数，同时把缺失项如实记录在 `missing` 中。

本模块是 `market_daily.sentiment_score` 的唯一生产者（`apply_sentiment_score`）。
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy.orm import Session

from app.models.market import MarketDaily

logger = logging.getLogger("app.sentiment")

SENTIMENT_RULE_VERSION = "v1"

# 分项满分（合计 100）
BREADTH_FULL = 40.0  # 涨跌广度
LIMIT_FULL = 30.0    # 涨停强度
MONEY_FULL = 30.0    # 资金面

# 资金面分档：主力资金净流入（亿元）≥ 阈值下限即取对应得分
MONEY_TIERS: tuple[tuple[float, float], ...] = (
    (100.0, 30.0),
    (30.0, 24.0),
    (0.0, 18.0),
    (-30.0, 12.0),
    (-100.0, 6.0),
)
MONEY_MISSING_SCORE = 0.0  # 低于最低档（净流出 ≥100 亿元）

# 定性档位：(下限, 名称)，降序匹配
LEVELS: tuple[tuple[float, str], ...] = (
    (70.0, "强势"),
    (55.0, "偏强"),
    (45.0, "中性"),
    (30.0, "偏弱"),
)

# 涨跌停家数均为 0 时涨停强度取中性分（无法区分强弱）
LIMIT_NEUTRAL_SCORE = 15.0


def _breadth_score(up_count: int | None, down_count: int | None) -> tuple[float | None, str]:
    if up_count is None or down_count is None:
        return None, "上涨/下跌家数缺失"
    total = int(up_count) + int(down_count)
    if total <= 0:
        return None, "涨跌家数合计为 0（平盘日或数据异常）"
    score = round(BREADTH_FULL * int(up_count) / total, 2)
    return score, f"{BREADTH_FULL:g} × {int(up_count)}/({int(up_count)}+{int(down_count)})"


def _limit_score(limit_up: int | None, limit_down: int | None) -> tuple[float | None, str]:
    if limit_up is None or limit_down is None:
        return None, "涨停/跌停家数缺失"
    up, down = int(limit_up), int(limit_down)
    if up + down <= 0:
        return LIMIT_NEUTRAL_SCORE, "涨跌停家数均为 0，取中性分"
    score = round(LIMIT_FULL * up / (up + down), 2)
    return score, f"{LIMIT_FULL:g} × {up}/({up}+{down})"


def _money_score(main_moneyflow: float | None) -> tuple[float | None, str]:
    if main_moneyflow is None:
        return None, "主力资金净流入缺失"
    yi = float(main_moneyflow) / 1e8
    for lower, tier_score in MONEY_TIERS:
        if yi >= lower:
            return tier_score, f"净流入 {yi:.2f} 亿元 ≥ {lower:g} 亿元 → {tier_score:g} 分"
    return MONEY_MISSING_SCORE, f"净流入 {yi:.2f} 亿元 < -100 亿元 → {MONEY_MISSING_SCORE:g} 分"


def level_of(score: float | None) -> str | None:
    """情绪分定性档位：≥70 强势 / ≥55 偏强 / ≥45 中性 / ≥30 偏弱 / 其余 弱势。"""
    if score is None:
        return None
    for lower, name in LEVELS:
        if score >= lower:
            return name
    return "弱势"


def compute_sentiment_score(
    *,
    up_count: int | None = None,
    down_count: int | None = None,
    limit_up_count: int | None = None,
    limit_down_count: int | None = None,
    main_moneyflow: float | None = None,
) -> dict:
    """按 v1 规则计算情绪分（纯函数，不触库；缺失分项折算百分制）。"""
    specs = (
        ("涨跌广度", BREADTH_FULL, _breadth_score(up_count, down_count)),
        ("涨停强度", LIMIT_FULL, _limit_score(limit_up_count, limit_down_count)),
        ("资金面", MONEY_FULL, _money_score(main_moneyflow)),
    )
    parts: dict[str, dict] = {}
    missing: list[str] = []
    for name, full, (score, note) in specs:
        if score is None:
            missing.append(name)
            continue
        parts[name] = {"full": full, "score": score, "note": note}

    if not parts:
        total = None
    else:
        got = sum(p["score"] for p in parts.values())
        full = sum(p["full"] for p in parts.values())
        total = round(100.0 * got / full, 1)
    return {
        "version": SENTIMENT_RULE_VERSION,
        "score": total,
        "level": level_of(total),
        "parts": parts,
        "missing": missing,
    }


def score_of_market(market: MarketDaily | None) -> dict:
    """按 market_daily 行计算情绪分（不落库）。"""
    if market is None:
        return compute_sentiment_score()
    return compute_sentiment_score(
        up_count=market.up_count,
        down_count=market.down_count,
        limit_up_count=market.limit_up_count,
        limit_down_count=market.limit_down_count,
        main_moneyflow=market.main_moneyflow,
    )


def apply_sentiment_score(db: Session, day: date) -> dict:
    """计算并写回 market_daily.sentiment_score（合并 extra 记录规则版本与分项）。

    行不存在时返回计算结果但不建行（避免为无数据日写入空记录）。
    """
    market = db.get(MarketDaily, day)
    if market is None:
        result = compute_sentiment_score()
        result["applied"] = False
        logger.warning("market_daily 无 %s 行，情绪分不落库", day.isoformat())
        return result

    result = score_of_market(market)
    market.sentiment_score = result["score"]
    extra = dict(market.extra or {})
    extra["sentiment_rule_version"] = result["version"]
    extra["sentiment_parts"] = {k: v["score"] for k, v in result["parts"].items()}
    if result["missing"]:
        extra["sentiment_missing"] = list(result["missing"])
    market.extra = extra
    db.commit()
    result["applied"] = True
    return result