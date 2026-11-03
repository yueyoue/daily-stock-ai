"""测试用假采集器（tests 引用 impl_ref: "tests.fake_sources:XXX"）。"""


class FakeOkCollector:
    def fetch(self, **params):
        return [{"code": "000001", "date": "2026-11-03"}]


class FakeFailCollector:
    def fetch(self, **params):
        raise RuntimeError("模拟接口失效")


class FakeEchoCollector:
    def fetch(self, **params):
        return [{"params": params}]
