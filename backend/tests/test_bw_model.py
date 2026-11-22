"""边风炜估值模型（bw_model / valuation）口径测试。

覆盖：
1. 行业 → 风格映射（银行/白酒/煤炭/半导体/地产/未分类）
2. 财报可信度 gate（含金融行业负债率豁免）
3. 硬门槛 + 评分 + 档位（通过 / 观察 / 排除）
4. 估值快照：股本由 equity/bvps 反推、PE 年化与 PB 口径
"""
from datetime import date

import pytest

from app.models.base import SessionLocal, init_db
from app.models.market import StockFinIndicator, StockInfo, StockKline
from app.services import bw_model
from app.services.valuation import build_snapshot


# ---------- 1. 风格映射 ----------
@pytest.mark.parametrize("industry,expect", [
    ("银行", "bank"),
    ("保险", "insurance"),
    ("酿酒行业", "consumer"),
    ("白色家电", "consumer"),
    ("煤炭行业", "cyclical"),
    ("半导体", "growth"),
    ("计算机设备", "growth"),
    ("房地产开发", "property"),
    ("电力行业", "utility"),
    ("综合类", "other"),
])
def test_style_of(industry, expect):
    assert bw_model.style_of(industry) == expect


# ---------- 2. gate 与行业豁免 ----------
def test_gate_penalises_poor_cashflow():
    r = bw_model.credibility({"ocf_to_profit": 0.4, "deduct_ratio": 60})
    assert r["score"] <= 60
    assert r["verdict"] == "reject"
    assert any("净现比" in x for x in r["red_flags"])


def test_gate_exempts_bank_leverage():
    """银行负债率 92%、速动 0.2 属经营模式，不应扣分。"""
    clean = bw_model.credibility({"debt_to_asset": 92.0, "quick_ratio": 0.2}, style="bank")
    assert clean["score"] == 100.0
    # 同一组数值在制造业（growth）就要扣分
    dirty = bw_model.credibility({"debt_to_asset": 92.0, "quick_ratio": 0.2}, style="growth")
    assert dirty["score"] < 100.0


def test_gate_detects_profit_cash_divergence():
    r = bw_model.credibility({"profit_yoy": 30.0, "ocf_yoy": -20.0})
    assert r["score"] < 100.0
    assert any("背离" in x for x in r["red_flags"])


# ---------- 3. 单票评估 ----------
def _bank_metrics(**over):
    base = {
        "price": 44.0, "eps": 5.4, "bvps": 46.0, "share": 2.0e10,
        "pe_annualized": 6.1, "pe_static": 7.0, "pb": 0.96,
        "div_yield": None, "roe": 15.0, "revenue_yoy": 3.0,
        "ocf_to_profit": 1.2, "deduct_ratio": 100.0,
        "debt_to_asset": 92.0, "quick_ratio": 0.2,
        "report_date": "2026-09-30",
    }
    base.update(over)
    return base


def test_evaluate_hard_pass():
    out = bw_model.evaluate_one("600036", "招商银行", "银行", _bank_metrics())
    assert out["style"] == "bank"
    assert out["verdict"] in ("pass", "watch")
    # 股息率缺失 → 该门槛 skipped，不计入失败
    skipped = [c for c in out["checks"] if c.get("skipped")]
    assert any(c["factor"] == "div_yield" for c in skipped)


def test_evaluate_rejects_on_gate():
    m = _bank_metrics(ocf_to_profit=0.3, deduct_ratio=50.0)
    out = bw_model.evaluate_one("000001", "平安银行", "银行", m)
    assert out["verdict"] == "reject"
    assert "财报可信度" in out["decision"]
    assert out["credibility"] < bw_model.GATE_REJECT


def test_evaluate_pe_band_out_of_range():
    """消费股 PE 明显高于区间上沿 → 应被硬门槛排除。"""
    m = {"price": 100.0, "eps": 1.0, "bvps": 10.0, "pe_annualized": 60.0,
         "pb": 10.0, "div_yield": 4.0, "roe": 25.0, "revenue_yoy": 10.0,
         "ocf_to_profit": 1.1, "deduct_ratio": 95.0, "report_date": "2026-09-30"}
    out = bw_model.evaluate_one("600999", "某消费", "酿酒行业", m)
    assert out["verdict"] == "reject"
    assert any(c["factor"] == "pe_annualized" and c["pass"] is False for c in out["checks"])


# ---------- 4. 估值快照（DB） ----------
def setup_function():
    init_db()


def teardown_function():
    db = SessionLocal()
    try:
        for model in (StockKline, StockFinIndicator, StockInfo):
            db.query(model).delete()
        db.commit()
    finally:
        db.close()


def _seed(db):
    db.add(StockInfo(code="600036", name="招商银行", industry="银行"))
    db.add(StockKline(code="600036", date=date(2026, 11, 20), open=44, high=45, low=43,
                      close=44.0, volume=1e6, amount=1e7, adj_factor=1.0))
    db.add(StockFinIndicator(
        code="600036", report_date=date(2026, 9, 30), roe=15.0, gross_margin=None,
        net_margin=None, debt_to_asset=92.0, ocf_to_profit=1.2,
        metrics={"eps": 5.4, "bvps": 46.0, "equity": 9.2e11, "total_revenue": 3.0e11,
                 "net_profit": 1.0e11, "operating_cashflow": 1.2e11,
                 "revenue_yoy": 3.0, "profit_yoy": 2.0, "ocf_yoy": 5.0,
                 "deduct_ratio": 100.0, "quick_ratio": 0.2},
    ))
    db.commit()


def test_snapshot_derives_share_and_ratios():
    db = SessionLocal()
    try:
        _seed(db)
        snap = build_snapshot(db, codes=["600036"])
        row = snap["rows"]["600036"]
        # 股本 = equity / bvps
        assert row["share"] == pytest.approx(9.2e11 / 46.0, rel=1e-6)
        assert row["mkt_cap"] == pytest.approx(44.0 * 9.2e11 / 46.0, rel=1e-6)
        # PE 年化：eps 5.4（Q3 累计）× 4/3 = 7.2 → 44/7.2
        assert row["pe_annualized"] == pytest.approx(44.0 / 7.2, rel=1e-6)
        assert row["pb"] == pytest.approx(44.0 / 46.0, rel=1e-6)
        # 股息率无数据 → None（不猜）
        assert row["div_yield"] is None
        assert snap["meta"]["pe_type"] == "annualized"
    finally:
        db.close()


def test_scan_stats_and_filter():
    db = SessionLocal()
    try:
        _seed(db)
        res = bw_model.scan(db)
        assert res["meta"]["stats"]["total"] == 1
        assert res["items"][0]["code"] == "600036"
        # 按风格过滤
        assert bw_model.scan(db, style="consumer")["meta"]["filtered"] == 0
    finally:
        db.close()


def test_rules_meta_shape():
    meta = bw_model.rules_meta()
    assert meta["weights"]["valuation"] == 40
    assert {s["style"] for s in meta["styles"]} >= {"bank", "consumer", "cyclical", "growth"}