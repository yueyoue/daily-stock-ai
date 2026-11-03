"""T1.7 验收：K线采集（标准化/复权因子/upsert/断点续采规划/断网重跑续采/主源分页）。

mootdx/akshare 不在 CI 依赖内 → 假模块桩测采集器；真实数据抽查
（3 只标的与行情软件对齐、含 1 只退市）由部署后 T1.11 演练执行。
"""
import sys
import types
from datetime import date

from app.collectors.kline import (
    AkshareSinaKlineCollector,
    MootdxKlineCollector,
    normalize_kline_rows,
    xdxr_to_adj_factors,
)
from app.models.base import SessionLocal, init_db
from app.models.market import StockInfo, StockKline
from app.models.system import CollectProgress, DataSourceConfig
from app.services.kline import plan_ranges, sync_kline_for_code, upsert_kline
from app.services.source_registry import create_source

from .fake_sources import FakeKlineCollector


def setup_function():
    init_db()


def _clean(db):
    for model in (StockKline, StockInfo, CollectProgress, DataSourceConfig):
        db.query(model).delete()
    db.commit()


class FakeDF:
    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient):  # noqa: ARG002
        return [dict(r) for r in self._rows]


def _bar(day: str, close: float, vol: float = 5.0) -> dict:
    return {"datetime": f"{day} 00:00", "open": close, "high": close + 0.2,
            "low": close - 0.2, "close": close, "vol": vol, "amount": 100.0}


# ---------- 标准化 ----------

def test_normalize_aliases_factor_and_scale():
    rows = normalize_kline_rows(
        [
            {"日期": "2020-01-02", "开盘": 10, "最高": 11, "最低": 9, "收盘": 10.5,
             "成交量": 3, "成交额": 30},
            {"日期": "2020-01-01", "开盘": 10, "最高": 11, "最低": 9, "收盘": 10,
             "成交量": 2, "成交额": 20},
            {"日期": "2020-01-01", "开盘": 99, "最高": 99, "最低": 99, "收盘": 99},  # 去重先到先得
            {"日期": "2020-01-03", "开盘": "坏数据", "收盘": 1},  # 价格非法丢弃
        ],
        "600000",
        adj_factors={"2020-01-01": 1.01, "2020-01-02": 1.02},
        volume_scale=100.0,
    )
    assert [r["date"] for r in rows] == [date(2020, 1, 1), date(2020, 1, 2)]  # 升序+去重+丢坏行
    assert rows[0]["close"] == 10 and rows[0]["volume"] == 200.0  # 手→股 ×100
    assert rows[0]["adj_factor"] == 1.01 and rows[1]["adj_factor"] == 1.02


def test_xdxr_factor_cash_dividend():
    bars = [_bar(f"2020-01-0{i}", 10.0) for i in range(1, 6)]  # 收盘 10.0×5
    xdxr = [{"year": 2020, "month": 1, "day": 3, "category": 1,
             "fenhong": 1.0, "peigu": 0, "peigujia": 0, "songzhuangu": 0}]  # 10派1 → 每股0.1
    factors = xdxr_to_adj_factors(xdxr, bars)
    # 前收盘 10.0，参考价 10.0-0.1=9.9，调整比 10/9.9；除权日起（01-03）生效
    step = 10.0 / 9.9
    assert factors["2020-01-01"] == 1.0 and factors["2020-01-02"] == 1.0
    assert abs(factors["2020-01-03"] - step) < 1e-9
    assert abs(factors["2020-01-05"] - step) < 1e-9  # 累计因子维持


def test_xdxr_factor_songzhuangu_and_cumulative():
    bars = [_bar(f"2020-01-0{i}", 20.0) for i in range(1, 6)]
    xdxr = [
        {"year": 2020, "month": 1, "day": 2, "category": 1,
         "fenhong": 0, "peigu": 0, "peigujia": 0, "songzhuangu": 10},  # 10送10 → ×2
        {"year": 2020, "month": 1, "day": 4, "category": 1,
         "fenhong": 0, "peigu": 0, "peigujia": 0, "songzhuangu": 10},  # 再 10送10 → ×2
        {"year": 2020, "month": 1, "day": 5, "category": 6,
         "fenhong": 5, "peigu": 0, "peigujia": 0, "songzhuangu": 0},  # 非除权类忽略
    ]
    factors = xdxr_to_adj_factors(xdxr, bars)
    assert factors["2020-01-01"] == 1.0
    assert factors["2020-01-02"] == 2.0
    assert factors["2020-01-03"] == 2.0
    assert factors["2020-01-04"] == 4.0  # 累计 2×2
    assert factors["2020-01-05"] == 4.0


def test_xdxr_factor_uses_known_closes_fallback():
    bars = [_bar("2020-01-05", 10.0)]  # 增量小窗口：bars 不含事件前收盘
    xdxr = [{"year": 2020, "month": 1, "day": 3, "category": 1,
             "fenhong": 1.0, "peigu": 0, "peigujia": 0, "songzhuangu": 0}]
    factors = xdxr_to_adj_factors(xdxr, bars, known_closes={"2020-01-02": 10.1})
    assert abs(factors["2020-01-05"] - 1.01) < 1e-9
    # 算不出前收盘 → 跳过事件（因子 1.0），宁缺毋错
    factors2 = xdxr_to_adj_factors(xdxr, bars)
    assert factors2["2020-01-05"] == 1.0


# ---------- 入库 ----------

def test_upsert_insert_update_idempotent():
    db = SessionLocal()
    try:
        _clean(db)
        rec = {"code": "600000", "date": date(2020, 1, 2), "open": 10, "high": 11,
               "low": 9, "close": 10.5, "volume": 100, "amount": 1000, "adj_factor": 1.01}
        assert upsert_kline(db, [rec, dict(rec)])["inserted"] == 1  # 同批次去重
        rec2 = dict(rec, close=10.6, adj_factor=1.02)
        s = upsert_kline(db, [rec2])  # 幂等更新
        assert s == {"received": 1, "inserted": 0, "updated": 1, "dropped": 0}
        row = db.get(StockKline, ("600000", date(2020, 1, 2)))
        assert row.close == 10.6 and row.adj_factor == 1.02

        # 异常值过滤：高低价倒挂/价格非正丢弃并计数
        bad = upsert_kline(db, [
            {"code": "600000", "date": date(2020, 1, 3), "open": 10, "high": 9,
             "low": 11, "close": 10},
            {"code": "600000", "date": date(2020, 1, 4), "open": -1, "high": 1,
             "low": 1, "close": 1},
        ])
        assert bad["dropped"] == 2 and bad["inserted"] == 0
    finally:
        db.close()


# ---------- 断点续采规划 ----------

def test_plan_ranges_full_clamped_by_list_date():
    db = SessionLocal()
    try:
        _clean(db)
        db.add(StockInfo(code="600000", name="浦发银行", list_date=date(2020, 1, 1)))
        db.commit()
        plan = plan_ranges(db, "600000", end_date=date(2020, 1, 10))
        assert plan["mode"] == "full"
        assert plan["ranges"] == [{"start": date(2020, 1, 1), "end": date(2020, 1, 10)}]
    finally:
        db.close()


def test_plan_ranges_resume_head_gap_and_incremental():
    db = SessionLocal()
    try:
        _clean(db)
        db.add(StockInfo(code="600000", name="浦发银行", list_date=date(2020, 1, 1)))
        for i in range(5, 8):  # 只有 01-05~01-07（上轮中断留下的中段）
            db.add(StockKline(code="600000", date=date(2020, 1, i), open=1, high=1,
                              low=1, close=1, volume=0, amount=0, adj_factor=1))
        db.commit()
        plan = plan_ranges(db, "600000", end_date=date(2020, 1, 10))
        assert plan["mode"] == "resume"  # 头尾都有缺口
        assert plan["ranges"] == [
            {"start": date(2020, 1, 1), "end": date(2020, 1, 4)},
            {"start": date(2020, 1, 8), "end": date(2020, 1, 10)},
        ]
        # 续采记录 complete → 信任头部起点，只做尾部增量
        db.add(CollectProgress(code="600000", data_type="kline",
                               first_date=date(2020, 1, 5), last_date=date(2020, 1, 7),
                               status="complete"))
        db.commit()
        plan2 = plan_ranges(db, "600000", end_date=date(2020, 1, 10))
        assert plan2["mode"] == "incremental"
        assert plan2["ranges"] == [{"start": date(2020, 1, 8), "end": date(2020, 1, 10)}]
    finally:
        db.close()


# ---------- 同步（假采集器）----------

def test_sync_full_then_incremental_then_offline_rerun():
    db = SessionLocal()
    try:
        _clean(db)
        FakeKlineCollector.reset()
        db.add(StockInfo(code="600000", name="浦发银行", list_date=date(2020, 1, 1)))
        db.commit()
        create_source(db, data_type="kline", name="假源", impl_ref="tests.fake_sources:FakeKlineCollector",
                      priority=1)

        # ① 首次全量
        stats = sync_kline_for_code(db, "600000", end_date=date(2020, 1, 10))
        assert stats["mode"] == "full" and stats["inserted"] == 10
        assert FakeKlineCollector.calls[-1] == {
            "code": "600000", "start_date": "2020-01-01", "end_date": "2020-01-10"}

        # ② 模拟中断：断网失败 → 异常上抛但已有数据/进度保留（重跑可续）
        FakeKlineCollector.fail = True
        try:
            sync_kline_for_code(db, "600000", end_date=date(2020, 1, 15))
        except Exception:
            pass
        else:
            raise AssertionError("全部接口失败必须抛异常")
        prog = db.query(CollectProgress).filter_by(code="600000").one()
        assert prog.status == "complete"  # 上轮完整数据仍在

        # ③ 断网恢复重跑 → 只采缺失尾部（断点续采）
        FakeKlineCollector.fail = False
        stats = sync_kline_for_code(db, "600000", end_date=date(2020, 1, 15))
        assert stats["mode"] == "incremental"
        assert FakeKlineCollector.calls[-1]["start_date"] == "2020-01-11"
        assert FakeKlineCollector.calls[-1]["end_date"] == "2020-01-15"
        assert stats["inserted"] == 0 and stats["received"] == 0  # 假源数据止于 01-10
        prog = db.query(CollectProgress).filter_by(code="600000").one()
        assert prog.status == "complete"
    finally:
        db.close()


# ---------- 主源（假 mootdx 桩）----------

class FakeMootdxClient:
    def __init__(self, bars, xdxr):
        self._bars, self._xdxr, self.calls = bars, xdxr, 0

    def bars(self, symbol, frequency, start, offset):  # noqa: ARG002
        self.calls += 1
        return FakeDF(self._bars[start:start + offset])

    def xdxr(self, symbol):  # noqa: ARG002
        return FakeDF(self._xdxr)

    def close(self):
        pass


def test_mootdx_collector_pagination_and_factor(monkeypatch):
    bars = [_bar(f"2020-01-0{i}", 10.0) for i in range(1, 6)]
    xdxr = [{"year": 2020, "month": 1, "day": 3, "category": 1,
             "fenhong": 1.0, "peigu": 0, "peigujia": 0, "songzhuangu": 0}]
    client = FakeMootdxClient(bars, xdxr)

    quotes_mod = types.ModuleType("mootdx.quotes")

    class FakeFactory:
        @staticmethod
        def factory(market="std", **kw):  # noqa: ARG002
            return client

    quotes_mod.Quotes = FakeFactory
    mootdx_mod = types.ModuleType("mootdx")
    mootdx_mod.__path__ = []
    mootdx_mod.quotes = quotes_mod
    monkeypatch.setitem(sys.modules, "mootdx", mootdx_mod)
    monkeypatch.setitem(sys.modules, "mootdx.quotes", quotes_mod)
    monkeypatch.setattr(MootdxKlineCollector, "PAGE", 2)  # 强制翻页 5 根 ÷ 2 → 3 次

    rows = MootdxKlineCollector().fetch(code="600000",
                                        start_date="2020-01-01", end_date="2020-01-05")
    assert client.calls == 3  # 分页到底
    assert len(rows) == 5
    assert rows[0]["volume"] == 500.0  # 手→股
    assert rows[0]["adj_factor"] == 1.0 and abs(rows[2]["adj_factor"] - 10.0 / 9.9) < 1e-9
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)  # 升序


def test_akshare_sina_collector_factor_merge(monkeypatch):
    stub = types.ModuleType("akshare")

    def stock_zh_a_daily(symbol, start_date=None, end_date=None, adjust=""):  # noqa: ARG001
        if adjust == "hfq-factor":
            return FakeDF([{"date": "2020-01-01", "hfq_factor": 1.0},
                           {"date": "2020-01-02", "hfq_factor": 1.05}])
        return FakeDF([{"date": "2020-01-01", "open": 10, "high": 11, "low": 9,
                        "close": 10, "volume": 100},
                       {"date": "2020-01-02", "open": 10, "high": 11, "low": 9,
                        "close": 10.5, "volume": 200}])

    stub.stock_zh_a_daily = stock_zh_a_daily
    monkeypatch.setitem(sys.modules, "akshare", stub)

    rows = AkshareSinaKlineCollector().fetch(code="600000",
                                             start_date="2020-01-01", end_date="2020-01-02")
    assert rows[0]["adj_factor"] == 1.0 and rows[1]["adj_factor"] == 1.05
    assert rows[1]["volume"] == 200.0  # 新浪源本身为股，不缩放
