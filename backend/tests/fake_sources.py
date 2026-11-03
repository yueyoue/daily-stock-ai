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
    def rows_in_range(cls, start_date: str, end_date: str) -> list:
        out = []
        for i in range(cls.days):
            d = cls.start + timedelta(days=i)
            if start_date and d.isoformat() < start_date:
                continue
            if end_date and d.isoformat() > end_date:
                continue
            out.append({
                "code": cls.code, "date": d,
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
            params.get("start_date", ""), params.get("end_date", "")
        )
