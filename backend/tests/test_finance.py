"""T1.8 验收：财务数据采集（三表合并/关键科目映射/增量 upsert/备源转置解析）。

akshare 不在 CI 依赖内 → 假模块桩测采集器；真实数据验收
（抽 5 只标的关键科目与财报对数一致）由部署后 T1.11 演练执行。
"""
import sys
import types
from datetime import date

from app.collectors.finance import (
    AkshareEastsFinanceCollector,
    AkshareSinaFinanceCollector,
    merge_statement_rows,
)
from app.models.base import SessionLocal, init_db
from app.models.market import StockFinance
from app.models.system import DataSourceConfig
from app.services.finance import sync_finance_for_code, upsert_finance
from app.services.source_registry import create_source

from .fake_sources import FakeFinanceCollector


def setup_function():
    init_db()


def _clean(db):
    for model in (StockFinance, DataSourceConfig):
        db.query(model).delete()
    db.commit()


class FakeRows:
    """假 DataFrame（records 形态）。"""

    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient="records"):  # noqa: ARG002
        return [dict(r) for r in self._rows]


class FakeMatrix:
    """假 DataFrame（新浪矩阵形态：to_dict() → {列: {行: 值}}）。"""

    def __init__(self, matrix):
        self._matrix = matrix

    def to_dict(self):
        return self._matrix


# ---------- 三表合并与科目映射 ----------

def test_merge_statement_rows_maps_and_merges_by_report_date():
    sheets = {
        "profit": [{"REPORT_DATE": "2025-12-31 00:00:00", "TOTAL_OPERATE_INCOME": 100.5,
                    "PARENT_NETPROFIT": 10.2, "BASIC_EPS": 1.01, "OTHER_ITEM": 42}],
        "balance": [{"REPORT_DATE": "2025-12-31 00:00:00", "TOTAL_ASSETS": 1000,
                     "TOTAL_LIABILITIES": 400, "TOTAL_PARENT_EQUITY": 600}],
        "cashflow": [{"REPORT_DATE": "2025-12-31 00:00:00", "NETCASH_OPERATE": 33},
                     {"REPORT_DATE": "2025-09-30", "NETCASH_OPERATE": 20, "NETPROFIT": 7}],
    }
    recs = merge_statement_rows(sheets)
    assert [r["report_date"] for r in recs] == [date(2025, 9, 30), date(2025, 12, 31)]
    q4 = recs[1]
    assert q4["total_revenue"] == 100.5 and q4["net_profit"] == 10.2
    assert q4["operating_cashflow"] == 33 and q4["total_assets"] == 1000
    assert q4["equity"] == 600 and q4["eps"] == 1.01
    assert q4["extra"]["OTHER_ITEM"] == 42  # 白名单外科目进 extra（供 T2.1）
    q3 = recs[0]
    assert q3["net_profit"] == 7 and q3["total_revenue"] is None  # 缺科目为 None 不编造
    assert q3["report_type"] == "quarterly"


# ---------- 主源（东财三表）----------

def test_easts_finance_collector_with_stub(monkeypatch):
    stub = types.ModuleType("akshare")
    stub.stock_profit_sheet_by_report_em = lambda symbol: FakeRows(
        [{"REPORT_DATE": "2025-12-31", "TOTAL_OPERATE_INCOME": 100, "PARENT_NETPROFIT": 10}])
    stub.stock_balance_sheet_by_report_em = lambda symbol: FakeRows(
        [{"REPORT_DATE": "2025-12-31", "TOTAL_ASSETS": 1000},
         {"REPORT_DATE": "2025-06-30", "TOTAL_ASSETS": 800}])
    stub.stock_cash_flow_sheet_by_report_em = lambda symbol: FakeRows(
        [{"REPORT_DATE": "2025-12-31", "NETCASH_OPERATE": 33}])
    monkeypatch.setitem(sys.modules, "akshare", stub)

    rows = AkshareEastsFinanceCollector().fetch(code="600000", start_date="2025-07-01")
    assert [r["report_date"] for r in rows] == [date(2025, 12, 31)]  # 按报告期过滤
    assert rows[0]["code"] == "600000" and rows[0]["operating_cashflow"] == 33
    # 不过滤 → 两期
    rows_all = AkshareEastsFinanceCollector().fetch(code="600000")
    assert [r["report_date"] for r in rows_all] == [date(2025, 6, 30), date(2025, 12, 31)]


# ---------- 备源（新浪矩阵转置）----------

def test_sina_finance_collector_transposes_matrix(monkeypatch):
    stub = types.ModuleType("akshare")

    def stock_financial_report_sina(stock, symbol):  # noqa: ARG001
        if symbol == "利润表":
            return FakeMatrix({"2025-12-31": {"营业总收入": 100.0, "净利润": 10.0,
                                              "基本每股收益": 1.0}})
        return FakeMatrix({"2025-12-31": {"资产总计": 1000.0}})

    stub.stock_financial_report_sina = stock_financial_report_sina
    monkeypatch.setitem(sys.modules, "akshare", stub)

    rows = AkshareSinaFinanceCollector().fetch(code="600000")
    assert len(rows) == 1
    r = rows[0]
    assert r["code"] == "600000" and r["report_date"] == date(2025, 12, 31)
    assert r["total_revenue"] == 100.0 and r["net_profit"] == 10.0
    assert r["eps"] == 1.0 and r["total_assets"] == 1000.0  # 中文行项目映射生效


# ---------- 入库 ----------

def test_upsert_finance_insert_update_and_empty_guard():
    db = SessionLocal()
    try:
        _clean(db)
        rec = {"code": "600000", "report_date": date(2025, 12, 31), "report_type": "quarterly",
               "total_revenue": 100.0, "net_profit": 10.0, "operating_cashflow": None,
               "total_assets": 1000.0, "total_liabilities": 500.0, "equity": 500.0,
               "eps": 1.0, "extra": {"A": 1}}
        assert upsert_finance(db, [rec, dict(rec)])["inserted"] == 1  # 同批次去重

        # 空科目不覆盖 + extra 合并
        upsert_finance(db, [{"code": "600000", "report_date": date(2025, 12, 31),
                             "total_revenue": None, "net_profit": 11.0, "extra": {"B": 2}}])
        row = db.get(StockFinance, ("600000", date(2025, 12, 31)))
        assert row.total_revenue == 100.0 and row.net_profit == 11.0
        assert row.extra == {"A": 1, "B": 2}
    finally:
        db.close()


# ---------- 同步（假采集器）----------

def test_sync_finance_with_fake_collector_and_fallback():
    db = SessionLocal()
    try:
        _clean(db)
        FakeFinanceCollector.reset()
        create_source(db, data_type="finance", name="假财务源",
                      impl_ref="tests.fake_sources:FakeFinanceCollector", priority=1)

        stats = sync_finance_for_code(db, "600000")
        assert stats["inserted"] == 2 and stats["received"] == 2
        assert db.get(StockFinance, ("600000", date(2025, 12, 31))).eps == 1.0

        # 断网：采集链全挂 → 异常上抛（交由调度重试/告警）
        FakeFinanceCollector.fail = True
        try:
            sync_finance_for_code(db, "600000")
        except Exception:
            pass
        else:
            raise AssertionError("全部接口失败必须抛异常")
    finally:
        db.close()
