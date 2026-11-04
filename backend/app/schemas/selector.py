"""选股条件 DSL（任务 T5.1，开发文档 §4 模块4）。

条件组合 JSON schema：**财务因子/技术因子 × 比较符 × 阈值/区间，AND 组合**。

- 因子清单对齐 T2.1/T2.2：技术因子 = tech_indicators.FLAT_KEYS（28 个标量，
  模式 B 缓存覆盖），财务因子 = fin_indicators.METRIC_KEYS（30 项，库表粒度）
- 清单外但族内自定义参数（如 ma30/rsi24）→ 校验通过并标注 `needs_runtime`
  （提示走实时计算兜底，T5.2 执行器负责）；完全未知因子 → 明确校验错误
- 序列化/反序列化：to_json()/parse_scan_spec() 可复用存 stock_strategy.conditions
  （T5.3）与回测入参（T5.5+）

DSL JSON 示例（AND 组合）：
{
  "logic": "and",
  "conditions": [
    {"factor": "roe", "op": ">=", "value": 15},
    {"factor": "macd_hist", "op": "between", "value": -0.2, "value2": 0.2}
  ]
}
"""
from __future__ import annotations

import json
import math
import re

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.fin_indicators import METRIC_KEYS
from app.services.tech_indicators import FLAT_KEYS

# 因子全集：技术（缓存/实时可算） + 财务（库表报告期粒度）
TECH_FACTORS: tuple[str, ...] = FLAT_KEYS
FIN_FACTORS: tuple[str, ...] = METRIC_KEYS
KNOWN_FACTORS: frozenset[str] = frozenset(TECH_FACTORS) | frozenset(FIN_FACTORS)

# 族内自定义参数（清单外但可实时计算兜底，§5.3）：如 ma30 / rsi24 / ema50
_RUNTIME_FAMILY_RE = re.compile(r"^(ma|ema|rsi|atr|cci|wr|bias|roc|mtm|psy|vr)(\d+)$")

COMPARE_OPS: tuple[str, ...] = (">", ">=", "<", "<=", "==", "!=")
RANGE_OPS: tuple[str, ...] = ("between",)
ALL_OPS: tuple[str, ...] = COMPARE_OPS + RANGE_OPS


class FactorCondition(BaseModel):
    """单个因子条件：比较符 × 阈值；between 用 value~value2 闭区间。"""

    factor: str = Field(min_length=1, max_length=64)
    op: str
    value: float | None = None
    value2: float | None = None  # between 区间上界

    @field_validator("factor")
    @classmethod
    def _check_factor(cls, v: str) -> str:
        key = v.strip()
        if not key:
            raise ValueError("因子名不能为空")
        if key in KNOWN_FACTORS:
            return key
        if _RUNTIME_FAMILY_RE.match(key):
            return key  # 族内自定义参数：清单外，由 needs_runtime 标注兜底
        raise ValueError(
            f"未知因子: {key}（可用清单：28 个技术因子 + 30 个财务因子；"
            f"族内自定义参数如 ma30/rsi24 可用但需实时计算）"
        )

    @field_validator("op")
    @classmethod
    def _check_op(cls, v: str) -> str:
        if v not in ALL_OPS:
            raise ValueError(f"不支持的比较符: {v}（可用: {', '.join(ALL_OPS)}）")
        return v

    @model_validator(mode="after")
    def _check_values(self) -> "FactorCondition":
        if self.op in COMPARE_OPS:
            if self.value is None:
                raise ValueError(f"比较符 {self.op} 需要阈值 value")
            if self.value2 is not None:
                raise ValueError(f"比较符 {self.op} 不需要 value2（区间请用 between）")
        else:  # between
            if self.value is None or self.value2 is None:
                raise ValueError("between 需要区间下界 value 与上界 value2")
            if self.value > self.value2:
                raise ValueError(f"区间非法: value({self.value}) > value2({self.value2})")
        for name, num in (("value", self.value), ("value2", self.value2)):
            if num is not None and not math.isfinite(num):
                raise ValueError(f"{name} 必须是有限数值")
        return self

    @property
    def needs_runtime(self) -> bool:
        """清单外因子（族内自定义参数）→ 需实时计算兜底（§5.3）。"""
        return self.factor not in KNOWN_FACTORS

    def describe(self) -> str:
        """人读条件描述（结果页命中条件高亮用）。"""
        if self.op == "between":
            return f"{self.factor} ∈ [{self.value:g}, {self.value2:g}]"
        return f"{self.factor} {self.op} {self.value:g}"


class ScanSpec(BaseModel):
    """选股条件组合：AND 组合（一期仅支持 logic=and，文档 §4 模块4）。"""

    logic: str = Field(default="and")
    conditions: list[FactorCondition] = Field(min_length=1)

    @field_validator("logic")
    @classmethod
    def _check_logic(cls, v: str) -> str:
        if v != "and":
            raise ValueError("一期仅支持 AND 组合（logic='and'）")
        return v

    def runtime_factors(self) -> list[str]:
        """需要实时计算兜底的因子（清单外），供执行器标注（T5.2）。"""
        return [c.factor for c in self.conditions if c.needs_runtime]

    def to_json(self) -> str:
        """序列化（存 stock_strategy.conditions / 回测入参复用）。"""
        return self.model_dump_json()

    @classmethod
    def parse(cls, raw: str | dict) -> "ScanSpec":
        """反序列化：JSON 字符串或 dict → ScanSpec（非法条件抛 ValueError 明确错误）。"""
        data = json.loads(raw) if isinstance(raw, str) else raw
        return cls.model_validate(data)


def validate_conditions(raw: str | dict) -> dict:
    """校验报告（API 层友好输出）：{ok, errors, warnings, spec}。

    - 非法条件 → ok=False + 明确错误（不抛异常，供上层决定 400 信封）
    - 清单外因子 → warnings 提示"实时计算兜底"（校验仍通过）
    """
    try:
        spec = ScanSpec.parse(raw)
    except Exception as e:  # noqa: BLE001 pydantic 聚合错误统一转文本
        return {"ok": False, "errors": [str(e)], "warnings": [], "spec": None}
    warnings = [
        f"因子 {f} 不在指标缓存清单，将走实时计算兜底（稍慢但正确）"
        for f in spec.runtime_factors()
    ]
    return {"ok": True, "errors": [], "warnings": warnings, "spec": spec}
