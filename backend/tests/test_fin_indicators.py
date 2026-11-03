"""T2.1 验收：财务指标 30 项（口径/手算对账 5 只标的/增量不重复写/边界 None）。

手算对账：固定原始财报数 → 逐项人工推导期望值（注释给出算式），与计算结果比对。
口径全文见 app/services/fin_indicators.md。
"""
from datetime import date

from app.models.base import SessionLocal, init_db
from app.models.market import StockFinance, StockFinIndicator
from app.services.fin_indicators import (
    INDICATOR_SPEC,
    METRIC_KEYS,
    NOMINAL_COLUMNS,
    compute_fin_indicators_for_code,
    sync_fin_indicators,
    sync_fin_indicators_all,
)


def setup_function():
    init_db()


def _clean(db):
    for model in (StockFinIndicator, StockFinance):
        db.query(model).delete()
    db.commit()


def _add(db, code, d, rev, np_, ocf, ta, tl, eq, eps, extra=None):
    db.add(StockFinance(code=code, report_date=d, report_type="quarterly",
                        total_revenue=rev, net_profit=np_, operating_cashflow=ocf,
                        total_assets=ta, total_liabilities=tl, equity=eq, eps=eps,
                        extra=extra or {}))
    db.commit()


def _metrics(db, code, d):
    row = db.get(StockFinIndicator, (code, d))
    assert row is not None, f"{code} {d} 指标行缺失"
    return row


# ---------- 标的 A：全量 30 项手算对账 ----------

def _seed_code_a(db):
    """600001：5 个报告期，扩展科目齐全（东财英文列名）。"""
    rows = [
        (date(2024, 3, 31), 100, 10, 8, 1000, 400, 600, 0.5,
         {"OPERATE_COST": 60, "OPERATE_PROFIT": 12, "DEDUCT_PARENT_NETPROFIT": 9,
          "TOTAL_CURRENT_ASSETS": 500, "TOTAL_CURRENT_LIAB": 300, "INVENTORY": 100,
          "TOTAL_SHARE": 200, "RESEARCH_EXPENSE": 5,
          "NETCASH_INVEST": -20, "NETCASH_FINANCE": -5}),
        (date(2024, 6, 30), 250, 30, 25, 1050, 420, 630, 1.5,
         {"OPERATE_COST": 150, "OPERATE_PROFIT": 36, "DEDUCT_PARENT_NETPROFIT": 28,
          "TOTAL_CURRENT_ASSETS": 520, "TOTAL_CURRENT_LIAB": 310, "INVENTORY": 105,
          "TOTAL_SHARE": 200, "RESEARCH_EXPENSE": 12,
          "NETCASH_INVEST": -25, "NETCASH_FINANCE": -8}),
        (date(2024, 9, 30), 400, 55, 45, 1100, 430, 670, 2.75,
         {"OPERATE_COST": 240, "OPERATE_PROFIT": 65, "DEDUCT_PARENT_NETPROFIT": 52,
          "TOTAL_CURRENT_ASSETS": 540, "TOTAL_CURRENT_LIAB": 320, "INVENTORY": 110,
          "TOTAL_SHARE": 200, "RESEARCH_EXPENSE": 20,
          "NETCASH_INVEST": -30, "NETCASH_FINANCE": -10}),
        (date(2024, 12, 31), 600, 80, 70, 1200, 480, 720, 4.0,
         {"OPERATE_COST": 360, "OPERATE_PROFIT": 95, "DEDUCT_PARENT_NETPROFIT": 76,
          "TOTAL_CURRENT_ASSETS": 560, "TOTAL_CURRENT_LIAB": 330, "INVENTORY": 115,
          "TOTAL_SHARE": 200, "RESEARCH_EXPENSE": 30,
          "NETCASH_INVEST": -40, "NETCASH_FINANCE": -15}),
        (date(2025, 3, 31), 130, 12, 10, 1250, 500, 750, 0.6,
         {"OPERATE_COST": 78, "OPERATE_PROFIT": 15, "DEDUCT_PARENT_NETPROFIT": 11,
          "TOTAL_CURRENT_ASSETS": 580, "TOTAL_CURRENT_LIAB": 340, "INVENTORY": 120,
          "TOTAL_SHARE": 200, "RESEARCH_EXPENSE": 6.5,
          "NETCASH_INVEST": -22, "NETCASH_FINANCE": -6}),
    ]
    for d, rev, np_, ocf, ta, tl, eq, eps, extra in rows:
        _add(db, "600001", d, rev, np_, ocf, ta, tl, eq, eps, extra)


def test_hand_check_full_30_on_code_a():
    db = SessionLocal()
    try:
        _clean(db)
        _seed_code_a(db)
        items = compute_fin_indicators_for_code(db, "600001")
        assert len(items) == 5

        # ---- 2025-03-31：30 项逐项手算 ----
        m = items[-1]["metrics"]
        assert m["total_revenue"] == 130 and m["net_profit"] == 12
        assert m["deduct_net_profit"] == 11 and m["operating_cashflow"] == 10
        assert m["invest_cashflow"] == -22 and m["finance_cashflow"] == -6
        assert m["total_assets"] == 1250 and m["total_liabilities"] == 500
        assert m["equity"] == 750 and m["eps"] == 0.6
        assert m["bvps"] == 3.75          # 750 / 200
        assert m["ocf_ps"] == 0.05        # 10 / 200
        assert m["gross_margin"] == 40.0  # (130-78)/130
        assert abs(m["net_margin"] - 12 / 130 * 100) < 1e-3        # 9.2308
        assert abs(m["operating_margin"] - 15 / 130 * 100) < 1e-3  # 11.5385
        assert m["roe"] == 1.6            # 12/750
        assert m["roa"] == 0.96           # 12/1250
        assert m["debt_to_asset"] == 40.0 # 500/1250
        assert m["equity_ratio"] == 60.0  # 750/1250
        assert abs(m["current_ratio"] - 580 / 340) < 1e-3   # 1.7059 倍
        assert abs(m["quick_ratio"] - 460 / 340) < 1e-3     # (580-120)/340
        assert abs(m["ocf_to_profit"] - 10 / 12 * 100) < 1e-3   # 83.3333
        assert abs(m["ocf_to_revenue"] - 10 / 130 * 100) < 1e-3 # 7.6923
        assert abs(m["deduct_ratio"] - 11 / 12 * 100) < 1e-3    # 91.6667
        assert m["rd_expense_ratio"] == 5.0   # 6.5/130
        assert m["revenue_yoy"] == 30.0       # (130-100)/100
        assert m["profit_yoy"] == 20.0        # (12-10)/10
        assert m["ocf_yoy"] == 25.0           # (10-8)/8
        assert m["revenue_qoq"] == -35.0      # 单季130 vs 上单季(600-400)=200
        assert m["profit_qoq"] == -52.0       # 单季12 vs 上单季(80-55)=25

        # ---- 2024-06-30：单季环比口径 ----
        m2 = items[1]["metrics"]
        assert m2["gross_margin"] == 40.0     # (250-150)/250
        assert m2["revenue_qoq"] == 50.0      # 单季(250-100)=150 vs Q1 100
        assert m2["profit_qoq"] == 100.0      # 单季20 vs 10
        assert m2["revenue_yoy"] is None      # 无 2023 年数据

        # ---- 2024-03-31：Q1 单季=累计；环比无上期 → None ----
        m1 = items[0]["metrics"]
        assert m1["revenue_qoq"] is None and m1["profit_qoq"] is None
        assert abs(m1["roe"] - 10 / 600 * 100) < 1e-3  # 1.6667
    finally:
        db.close()


# ---------- 标的 B~E：抽样手算对账（验收"抽 5 只"） ----------

def test_hand_check_codes_b_to_e():
    db = SessionLocal()
    try:
        _clean(db)
        # B：正常双期
        _add(db, "000002", date(2024, 3, 31), 200, 20, 30, 2000, 1000, 1000, 1.0,
             {"OPERATE_COST": 140, "TOTAL_SHARE": 100})
        _add(db, "000002", date(2025, 3, 31), 220, 22, 28, 2200, 990, 1210, 1.1,
             {"OPERATE_COST": 154, "TOTAL_SHARE": 100})
        # C：亏损转盈、负分母增长率
        _add(db, "300003", date(2024, 3, 31), 50, -5, -2, 300, 200, 100, -0.25,
             {"OPERATE_COST": 40, "TOTAL_CURRENT_ASSETS": 150, "TOTAL_CURRENT_LIAB": 120,
              "INVENTORY": 30})
        _add(db, "300003", date(2025, 3, 31), 60, 6, 8, 320, 180, 140, 0.3,
             {"OPERATE_COST": 42, "TOTAL_CURRENT_ASSETS": 160, "TOTAL_CURRENT_LIAB": 110,
              "INVENTORY": 35})
        # D：扩展科目全缺（轻量备源口径）→ 对应指标 None
        _add(db, "688004", date(2024, 3, 31), 10, 1, 1, 100, 50, 50, 0.1)
        _add(db, "688004", date(2025, 3, 31), 12, 1.2, 1.5, 110, 55, 55, 0.12)
        # E：零分母 → None
        _add(db, "600005", date(2024, 3, 31), 0, 0, 0, 100, 0, 100, 0.0,
             {"OPERATE_COST": 0})
        _add(db, "600005", date(2025, 3, 31), 10, 2, 0, 100, 10, 90, 0.02,
             {"OPERATE_COST": 5})

        b = compute_fin_indicators_for_code(db, "000002")[-1]["metrics"]
        assert b["gross_margin"] == 30.0    # (220-154)/220
        assert b["net_margin"] == 10.0      # 22/220
        assert abs(b["roe"] - 22 / 1210 * 100) < 1e-3  # 1.8182
        assert b["debt_to_asset"] == 45.0   # 990/2200
        assert b["revenue_yoy"] == 10.0     # (220-200)/200
        assert b["bvps"] == 12.1            # 1210/100

        c25 = compute_fin_indicators_for_code(db, "300003")[-1]["metrics"]
        assert c25["gross_margin"] == 30.0  # (60-42)/60
        assert c25["profit_yoy"] == 220.0   # (6-(-5))/5，负基数取绝对值
        assert abs(c25["current_ratio"] - 160 / 110) < 1e-3   # 1.4545
        assert abs(c25["quick_ratio"] - 125 / 110) < 1e-3     # (160-35)/110
        assert abs(c25["ocf_to_profit"] - 8 / 6 * 100) < 1e-3  # 133.3333
        c24 = compute_fin_indicators_for_code(db, "300003")[0]["metrics"]
        assert c24["net_margin"] == -10.0   # -5/50
        assert c24["roe"] == -5.0           # -5/100

        d = compute_fin_indicators_for_code(db, "688004")[-1]["metrics"]
        assert d["net_margin"] == 10.0 and d["revenue_yoy"] == 20.0  # 定名列仍可算
        for key in ("gross_margin", "current_ratio", "quick_ratio", "bvps",
                    "deduct_net_profit", "rd_expense_ratio", "invest_cashflow"):
            assert d[key] is None, key  # 缺原始项 → None 而非 0

        e24 = compute_fin_indicators_for_code(db, "600005")[0]["metrics"]
        assert e24["net_margin"] is None       # 营收 0 → None
        assert e24["gross_margin"] is None     # 0/0 → None
        e25 = compute_fin_indicators_for_code(db, "600005")[-1]["metrics"]
        assert e25["revenue_yoy"] is None      # 基数 0 → None
        assert e25["gross_margin"] == 50.0     # (10-5)/10
    finally:
        db.close()


# ---------- 清单固化与落库 ----------

def test_spec_frozen_30_and_nominal_columns_filled():
    db = SessionLocal()
    try:
        _clean(db)
        _seed_code_a(db)
        assert len(INDICATOR_SPEC) == 30 and len(METRIC_KEYS) == 30
        assert len(set(METRIC_KEYS)) == 30

        sync_fin_indicators(db, "600001")
        row = _metrics(db, "600001", date(2025, 3, 31))
        assert set(row.metrics) == set(METRIC_KEYS)  # metrics=30 项全量
        # 5 个定名列与 metrics 同值（供 SQL 筛选）
        for col in NOMINAL_COLUMNS:
            assert getattr(row, col) == row.metrics[col]
    finally:
        db.close()


def test_incremental_second_run_no_rewrite():
    db = SessionLocal()
    try:
        _clean(db)
        _seed_code_a(db)
        s1 = sync_fin_indicators(db, "600001")
        assert s1 == {"code": "600001", "periods": 5, "written": 5, "skipped": 0}

        s2 = sync_fin_indicators(db, "600001")  # 重复跑增量
        assert s2 == {"code": "600001", "periods": 5, "written": 0, "skipped": 5}
        assert db.query(StockFinIndicator).filter_by(code="600001").count() == 5  # 不重复写
    finally:
        db.close()


def test_restatement_recomputes_dependent_periods():
    db = SessionLocal()
    try:
        _clean(db)
        _seed_code_a(db)
        sync_fin_indicators(db, "600001")

        # 财报更正：2024-03-31 净利 10 → 20（连带 2025-03-31 同比变化）
        row = db.get(StockFinance, ("600001", date(2024, 3, 31)))
        row.net_profit = 20.0
        db.commit()

        s = sync_fin_indicators(db, "600001")
        assert s["written"] >= 2 and s["skipped"] <= 3  # 变化期+受牵连期重写
        assert _metrics(db, "600001", date(2024, 3, 31)).metrics["net_profit"] == 20.0
        assert _metrics(db, "600001", date(2025, 3, 31)).metrics["profit_yoy"] == -40.0  # (12-20)/20
    finally:
        db.close()


def test_sync_all_batches_every_code():
    db = SessionLocal()
    try:
        _clean(db)
        _seed_code_a(db)
        _add(db, "000002", date(2024, 3, 31), 200, 20, 30, 2000, 1000, 1000, 1.0)
        totals = sync_fin_indicators_all(db)
        assert totals["codes"] == 2 and totals["written"] == 6 and totals["skipped"] == 0
    finally:
        db.close()
