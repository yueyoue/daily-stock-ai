"""测试用假采集器（tests 引用 impl_ref: "tests.fake_sources:XXX"）。"""
from datetime import date, timedelta


class FakeOkCollector:
    def fetch(self, **params):
        return [{"code": "000001", "date": "2026-11-03"}]


class FakeFailCollector:
    def fetch(self, **params):
        raise RuntimeError("模拟接口失效")


class FakeEchoCollector:
    def fetch(self, **params):
        return [{"params": params}]


class FakeStockInfoCollector:
    """假股票基础信息采集器（T1.10 流水线用）。"""

    def fetch(self, **params):
        return [
            {"code": "600000", "name": "股600000", "industry": "行业A",
             "concepts": ["概念A"], "market": "主板"},
        ]


class FakeMarketDataCollector:
    """假大盘汇总采集器（T1.9）：params_template 传 kind 区分三条线。"""

    calls: list = []
    fail_kinds: set = set()

    @classmethod
    def reset(cls):
        cls.calls = []
        cls.fail_kinds = set()

    def fetch(self, **params):
        FakeMarketDataCollector.calls.append(dict(params))
        kind = params.get("kind")
        if kind in FakeMarketDataCollector.fail_kinds:
            raise RuntimeError(f"模拟 {kind} 线失效")
        d = params.get("date", "2026-11-03")
        if kind == "moneyflow":
            return [{"date": d, "main_moneyflow": 123.4, "extra": {"超大单净流入": 50.0}}]
        if kind == "limit":
            return [{"date": d, "limit_up_count": 42, "limit_down_count": 7,
                     "extra": {"连板梯队": {"1板": 30, "2板": 8, "3板": 4}, "最高连板": 3}}]
        return [{"date": d, "up_count": 3000, "down_count": 2000,
                 "extra": {"flat_count": 200}}]


class FakeFinanceCollector:
    """可配置假财务采集器（T1.8）。"""

    calls: list = []
    fail = False

    @classmethod
    def reset(cls):
        cls.calls = []
        cls.fail = False

    def fetch(self, **params):
        FakeFinanceCollector.calls.append(dict(params))
        if FakeFinanceCollector.fail:
            raise RuntimeError("模拟接口失效")
        code = params.get("code", "600000")
        return [
            {"code": code, "report_date": date(2025, 12, 31), "report_type": "quarterly",
             "total_revenue": 100.0, "net_profit": 10.0, "operating_cashflow": 30.0,
             "total_assets": 1000.0, "total_liabilities": 500.0, "equity": 500.0,
             "eps": 1.0, "extra": {"ROE_RAW": 20.0}},
            {"code": code, "report_date": date(2025, 9, 30), "report_type": "quarterly",
             "total_revenue": 80.0, "net_profit": 8.0, "operating_cashflow": 20.0,
             "total_assets": 900.0, "total_liabilities": 450.0, "equity": 450.0,
             "eps": 0.8, "extra": {"ROE_RAW": 18.0}},
        ]


class FakeKlineCollector:
    """可配置假 K 线采集器（T1.7）：类级状态记录调用参数/控制产出与失败。"""

    calls: list = []          # 每次 fetch 的 params
    fail = False              # True → 模拟断网
    base_price = 10.0
    code = "600000"
    start = date(2020, 1, 1)
    days = 10

    @classmethod
    def reset(cls, **kw):
        cls.calls = []
        cls.fail = False
        for k, v in kw.items():
            setattr(cls, k, v)

    @classmethod
    def rows_in_range(cls, start_date: str, end_date: str, code: str | None = None) -> list:
        out = []
        for i in range(cls.days):
            d = cls.start + timedelta(days=i)
            if start_date and d.isoformat() < start_date:
                continue
            if end_date and d.isoformat() > end_date:
                continue
            out.append({
                "code": code or cls.code, "date": d,
                "open": cls.base_price, "high": cls.base_price + 1,
                "low": cls.base_price - 1, "close": cls.base_price + i * 0.1,
                "volume": 1000.0, "amount": 10000.0, "adj_factor": 1.0 + i * 0.01,
            })
        return out

    def fetch(self, **params):
        FakeKlineCollector.calls.append(dict(params))
        if FakeKlineCollector.fail:
            raise RuntimeError("模拟断网/接口失效")
        return FakeKlineCollector.rows_in_range(
            params.get("start_date", ""), params.get("end_date", ""),
            code=params.get("code"),
        )
