"""T1.9 验收：资金流向/涨跌停/大盘汇总采集（三线合成 market_daily、东财 QPS 配置限流）。

akshare 不在 CI 依赖内 → 假模块桩测采集器；东财调用频率由 data_source_config
qps_limit + 限流器保证（T1.3 已验），此处登记项 QPS≤1 见 source_seed。
"""
import sys
import types
from datetime import date

from app.collectors.limit import AkshareEastsLimitPoolCollector, AkshareSpotLimitCollector
from app.collectors.market import AkshareEastsSpotSummaryCollector, AkshareLeguActivityCollector
from app.collectors.moneyflow import (
    AkshareEastsFundFlowRankCollector,
    AkshareEastsMarketFundFlowCollector,
)
from app.models.base import SessionLocal, init_db
from app.models.market import MarketDaily
from app.models.system import DataSourceConfig
from app.services.market import sync_market_daily, upsert_market_daily
from app.services.source_registry import create_source

from .fake_sources import FakeMarketDataCollector


def setup_function():
    init_db()


def _clean(db):
    for model in (MarketDaily, DataSourceConfig):
        db.query(model).delete()
    db.commit()


class FakeRows:
    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient="records"):  # noqa: ARG002
        return [dict(r) for r in self._rows]


class FakeIndexedRows:
    """模拟日期在索引上的 DataFrame（to_dict("records") 丢索引 → reset_index 兜底）。"""

    def __init__(self, rows, indexed_rows=None):
        self._rows, self._indexed = rows, indexed_rows

    def to_dict(self, orient="records"):  # noqa: ARG002
        return [dict(r) for r in self._rows]

    def reset_index(self):
        return FakeRows(self._indexed)


def test_moneyflow_market_collector_filters_by_date(monkeypatch):
    stub = types.ModuleType("akshare")
    stub.stock_market_fund_flow = lambda: FakeIndexedRows(
        [{"主力净流入-净额": 999}],  # 索引布局：records 无日期列
        indexed_rows=[{"日期": "2026-11-02", "主力净流入-净额": 888},
                      {"日期": "2026-11-03", "主力净流入-净额": 123.4,
                       "超大单净流入-净额": 50}],
    )
    monkeypatch.setitem(sys.modules, "akshare", stub)
    rows = AkshareEastsMarketFundFlowCollector().fetch(date="2026-11-03")
    assert rows == [{"date": date(2026, 11, 3), "main_moneyflow": 123.4,
                     "extra": {"超大单净流入": 50.0}}]


def test_moneyflow_rank_collector_sums_fallback(monkeypatch):
    stub = types.ModuleType("akshare")
    stub.stock_individual_fund_flow_rank = lambda indicator: FakeRows([
        {"代码": "600000", "今日主力净流入-净额": 100.5},
        {"代码": "000001", "今日主力净流入-净额": -20.5},
    ])
    monkeypatch.setitem(sys.modules, "akshare", stub)
    rows = AkshareEastsFundFlowRankCollector().fetch(date="2026-11-03")
    assert rows[0]["main_moneyflow"] == 80.0  # 加总兜底


def test_limit_pool_collector_counts_and_lianban(monkeypatch):
    stub = types.ModuleType("akshare")
    stub.stock_zt_pool_em = lambda date: FakeRows([
        {"代码": "600001", "名称": "甲", "连板数": 3},
        {"代码": "600002", "名称": "乙", "连板数": 1},
        {"代码": "600003", "名称": "丙", "连板数": 1},
    ])
    stub.stock_zt_pool_dtgc_em = lambda date: FakeRows([{"代码": "600009", "名称": "丁"}])
    monkeypatch.setitem(sys.modules, "akshare", stub)
    rows = AkshareEastsLimitPoolCollector().fetch(date="2026-11-03")
    r = rows[0]
    assert r["limit_up_count"] == 3 and r["limit_down_count"] == 1
    assert r["extra"]["连板梯队"] == {"1板": 2, "3板": 1}
    assert r["extra"]["最高连板"] == 3
    assert r["extra"]["高连板股"][0] == {"code": "600001", "name": "甲", "连板数": 3}


def test_limit_spot_fallback_and_market_summary(monkeypatch):
    stub = types.ModuleType("akshare")
    stub.stock_zh_a_spot_em = lambda: FakeRows([
        {"涨跌幅": 10.02, "成交额": 100}, {"涨跌幅": -9.95, "成交额": 200},
        {"涨跌幅": 5.0, "成交额": 300}, {"涨跌幅": -1.0, "成交额": 400},
        {"涨跌幅": 0.0, "成交额": 500},
    ])
    monkeypatch.setitem(sys.modules, "akshare", stub)
    limit = AkshareSpotLimitCollector().fetch(date="2026-11-03")[0]
    assert limit["limit_up_count"] == 1 and limit["limit_down_count"] == 1
    assert "兜底" in limit["extra"]["涨跌停口径"]

    summary = AkshareEastsSpotSummaryCollector().fetch(date="2026-11-03")[0]
    assert summary["up_count"] == 2 and summary["down_count"] == 2
    assert summary["extra"]["flat_count"] == 1 and summary["extra"]["turnover_amount"] == 1500


def test_legu_activity_backup_uses_latest_row(monkeypatch):
    stub = types.ModuleType("akshare")
    stub.stock_market_activity_legu = lambda: FakeIndexedRows(
        [], indexed_rows=[{"日期": "2026-11-02", "上涨家数": 2800, "下跌家数": 2100,
                           "平盘家数": 200, "赚钱效应": 66.6}],
    )
    monkeypatch.setitem(sys.modules, "akshare", stub)
    rows = AkshareLeguActivityCollector().fetch(date="2026-11-03")
    assert rows[0]["up_count"] == 2800 and rows[0]["down_count"] == 2100
    assert rows[0]["extra"]["flat_count"] == 200 and rows[0]["extra"]["赚钱效应"] == 66.6
    assert rows[0]["date"] == date(2026, 11, 2)  # 滞后取最新一期


def test_upsert_market_daily_merges_partial_rows():
    db = SessionLocal()
    try:
        _clean(db)
        upsert_market_daily(db, [
            {"date": date(2026, 11, 3), "main_moneyflow": 123.4, "extra": {"超大单净流入": 50}},
            {"date": date(2026, 11, 3), "limit_up_count": 42, "limit_down_count": 7},
            {"date": date(2026, 11, 3), "up_count": 3000, "down_count": 2000,
             "extra": {"flat_count": 200}},
        ])
        row = db.get(MarketDaily, date(2026, 11, 3))
        assert (row.main_moneyflow, row.limit_up_count, row.limit_down_count) == (123.4, 42, 7)
        assert (row.up_count, row.down_count) == (3000, 2000)
        assert row.extra == {"超大单净流入": 50, "flat_count": 200}

        # 空值不覆盖
        upsert_market_daily(db, [{"date": date(2026, 11, 3), "main_moneyflow": None}])
        db.refresh(row)
        assert row.main_moneyflow == 123.4
    finally:
        db.close()


def test_sync_market_daily_complete_and_partial_failure():
    db = SessionLocal()
    try:
        _clean(db)
        FakeMarketDataCollector.reset()
        for kind, name in [("moneyflow", "假资金线"), ("limit", "假涨跌停线"),
                           ("market", "假家数线")]:
            create_source(db, data_type=kind, name=name,
                          impl_ref="tests.fake_sources:FakeMarketDataCollector",
                          params_template={"kind": kind}, priority=1)

        # 三线全通 → 字段齐全
        stats = sync_market_daily(db, day=date(2026, 11, 3),
                                  data_types=("moneyflow", "limit", "market"))
        assert stats["complete"] is True and not stats["errors"]
        row = db.get(MarketDaily, date(2026, 11, 3))
        assert (row.main_moneyflow, row.limit_up_count, row.limit_down_count,
                row.up_count, row.down_count) == (123.4, 42, 7, 3000, 2000)

        # 单线失败：其余照常入库，errors 汇总
        FakeMarketDataCollector.reset()
        FakeMarketDataCollector.fail_kinds = {"limit"}
        stats = sync_market_daily(db, day=date(2026, 11, 4),
                                  data_types=("moneyflow", "limit", "market"))
        assert stats["complete"] is False and len(stats["errors"]) == 1
        row = db.get(MarketDaily, date(2026, 11, 4))
        assert row.main_moneyflow == 123.4 and row.limit_up_count is None

        # 全挂 → 502
        FakeMarketDataCollector.reset()
        FakeMarketDataCollector.fail_kinds = {"moneyflow", "limit", "market"}
        try:
            sync_market_daily(db, day=date(2026, 11, 5),
                              data_types=("moneyflow", "limit", "market"))
        except Exception as e:
            assert "全部采集线失败" in str(e)
        else:
            raise AssertionError("全部采集线失败必须抛异常")
    finally:
        db.close()
