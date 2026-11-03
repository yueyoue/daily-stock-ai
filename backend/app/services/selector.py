"""选股扫描服务（T3.3 API 层骨架；全市场扫描执行器 T5.2 完整实现）。

- conditions：[{factor, op, value}] AND 组合（T5.1 将扩展区间/财务因子 DSL）
- 目前基于 T2.5 scan_factor（指标缓存快照）；缺缓存慢路径兜底已内建
- **20 秒预算看门狗**（§4 模块4）：超预算抛 504，明确错误返回
"""
from __future__ import annotations

import logging
import time

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.services.indicator_query import _OPS as SCAN_OPS, scan_factor
from app.services.tech_indicators import FLAT_KEYS

logger = logging.getLogger("app.selector")

_OPS = (">", ">=", "<", "<=", "==", "!=")


def _ensure_budget(deadline: float, budget_s: float) -> None:
    if time.monotonic() > deadline:
        raise AppError(504, f"扫描超时（超过 {budget_s:g}s 预算），请收紧条件后重试")


def run_scan(db: Session, conditions: list[dict], trade_date=None) -> dict:
    """执行扫描，返回候选池 + 匹配条件明细（同步，超预算 504）。"""
    if not conditions:
        raise AppError(400, "conditions 不能为空")
    for c in conditions:
        if c.get("factor") not in FLAT_KEYS:
            raise AppError(400, f"未知因子: {c.get('factor')}；"
                                "T5.1 前仅支持技术因子（INDICATOR_SPEC 清单）")
        if c.get("op") not in _OPS:
            raise AppError(400, f"未知比较符: {c.get('op')}；可用 {list(_OPS)}")

    budget_s = float(get_settings().selector_scan_budget_s)
    started, deadline = time.monotonic(), time.monotonic() + budget_s

    # 首条件走缓存索引（scan_factor），其余条件行级 AND 过滤
    first, rest = conditions[0], conditions[1:]
    rows = scan_factor(db, first["factor"], first["op"], first["value"],
                       trade_date=trade_date)
    _ensure_budget(deadline, budget_s)
    for c in rest:
        fn = SCAN_OPS[c["op"]]
        rows = [r for r in rows if r.get(c["factor"]) is not None
                and fn(r[c["factor"]], c["value"])]
        _ensure_budget(deadline, budget_s)

    candidates = [
        {
            "code": r["code"],
            "date": r.get("date"),
            "matched": [f"{c['factor']} {c['op']} {c['value']}" for c in conditions],
            "metrics": {c["factor"]: r.get(c["factor"]) for c in conditions},
        }
        for r in rows
    ]
    scan_ms = int((time.monotonic() - started) * 1000)
    logger.info("选股扫描：%d 条件，%d 候选，耗时 %dms（预算 %.0fs）",
                len(conditions), len(candidates), scan_ms, budget_s)
    return {"scan_ms": scan_ms, "budget_s": budget_s,
            "conditions": conditions, "total": len(candidates),
            "candidates": candidates}
