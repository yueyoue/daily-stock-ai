"""每接口 QPS 限流（开发文档 §2.4.1：QPS 上限登记在接口配置表）。"""
import time
from collections import defaultdict


class RateLimiter:
    """最小间隔限流：同一 key 两次调用间隔 ≥ 1/qps 秒。clock/sleep 可注入便于测试。"""

    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self._last: dict[str, float] = defaultdict(float)
        self._clock = clock
        self._sleep = sleep

    def wait(self, key: str, qps: float) -> None:
        if qps is None or qps <= 0:
            return
        min_interval = 1.0 / qps
        delta = self._clock() - self._last[key]
        if delta < min_interval:
            self._sleep(min_interval - delta)
        self._last[key] = self._clock()
