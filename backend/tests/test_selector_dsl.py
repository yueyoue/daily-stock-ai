"""T5.1 验收：选股条件 DSL（非法条件明确错误 / 合法条件可序列化复用 / 清单对齐校验）。"""
import pytest
from pydantic import ValidationError

from app.schemas.selector import (
    FIN_FACTORS,
    KNOWN_FACTORS,
    TECH_FACTORS,
    FactorCondition,
    ScanSpec,
    validate_conditions,
)


def test_factor_universe_alignment():
    """与 T2.1/T2.2 清单对齐：28 技术 + 30 财务，全部可作因子。"""
    assert len(TECH_FACTORS) == 28 and len(FIN_FACTORS) == 30
    spec = {
        "logic": "and",
        "conditions": [{"factor": f, "op": ">=", "value": 1} for f in sorted(KNOWN_FACTORS)],
    }
    report = validate_conditions(spec)
    assert report["ok"] and not report["warnings"]  # 清单内因子无兜底提示


def test_round_trip_serialize_reuse():
    raw = {
        "logic": "and",
        "conditions": [
            {"factor": "roe", "op": ">=", "value": 15},
            {"factor": "macd_hist", "op": "between", "value": -0.2, "value2": 0.2},
        ],
    }
    spec = ScanSpec.parse(raw)
    again = ScanSpec.parse(spec.to_json())  # 序列化→反序列化复用（策略保存/回测入参）
    assert again.model_dump() == spec.model_dump()
    assert [c.describe() for c in spec.conditions] == [
        "roe >= 15", "macd_hist ∈ [-0.2, 0.2]",
    ]


def test_compare_and_range_semantics():
    c = FactorCondition(factor="ma5", op=">", value=10.5)
    assert not c.needs_runtime
    r = FactorCondition(factor="rsi14", op="between", value=30, value2=70)
    assert r.value == 30 and r.value2 == 70


def test_unknown_factor_has_clear_error():
    report = validate_conditions({"conditions": [{"factor": "volatility_30d", "op": ">", "value": 1}]})
    assert not report["ok"] and "未知因子" in report["errors"][0]
    with pytest.raises(ValidationError) as ei:
        FactorCondition(factor="no_such", op=">", value=1)
    assert "未知因子" in str(ei.value)


def test_runtime_family_factor_warns_fallback():
    """族内自定义参数（清单外）→ 校验通过 + 实时计算兜底提示（§5.3）。"""
    report = validate_conditions({"conditions": [{"factor": "ma30", "op": ">", "value": 5}]})
    assert report["ok"] and len(report["warnings"]) == 1
    assert "实时计算兜底" in report["warnings"][0]
    assert ScanSpec.parse({"conditions": [{"factor": "rsi24", "op": "<", "value": 30}]}).runtime_factors() == ["rsi24"]


@pytest.mark.parametrize(
    "cond,keyword",
    [
        ({"factor": "roe", "op": "~=", "value": 1}, "比较符"),          # 非法操作符
        ({"factor": "roe", "op": ">="}, "阈值"),                        # 缺阈值
        ({"factor": "roe", "op": ">=", "value": 1, "value2": 2}, "value2"),  # 比较符带区间
        ({"factor": "rsi14", "op": "between", "value": 30}, "value2"),  # 缺上界
        ({"factor": "rsi14", "op": "between", "value": 70, "value2": 30}, "区间非法"),  # 倒挂
    ],
)
def test_invalid_conditions_clear_errors(cond, keyword):
    report = validate_conditions({"conditions": [cond]})
    assert not report["ok"] and keyword in report["errors"][0]


def test_empty_and_non_and_logic_rejected():
    assert not validate_conditions({"conditions": []})["ok"]
    assert "AND" in validate_conditions({"logic": "or", "conditions": [{"factor": "roe", "op": ">", "value": 1}]})["errors"][0]


def test_non_finite_value_rejected():
    report = validate_conditions({"conditions": [{"factor": "roe", "op": ">", "value": float("nan")}]})
    assert not report["ok"] and "有限" in report["errors"][0]
