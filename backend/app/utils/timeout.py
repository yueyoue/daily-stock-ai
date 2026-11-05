"""调用超时包装（开发文档 §2.4.1：接口配置 timeout_sec 必须真实生效）。

采集接口多为同步阻塞的 requests/akshare 调用，无法被强制中断，故用
「单线程池 + future.result(timeout)」在调用方一侧施加截止时间：
超时即抛 TimeoutError 交由采集链降级，后台线程自然结束后回收
（shutdown(wait=False)，不阻塞调用方）。
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

logger = logging.getLogger("app.utils.timeout")


def call_with_timeout(fn, timeout_sec: float | None = None):
    """在 timeout_sec 秒内执行 fn()；超时抛 TimeoutError。

    timeout_sec 为 None / <= 0 时直接同步执行（视为不限时）。
    """
    if timeout_sec is None or timeout_sec <= 0:
        return fn()
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn)
    try:
        return future.result(timeout=timeout_sec)
    except FuturesTimeout:
        future.cancel()
        logger.warning("接口调用超时（>%.0fs），放弃等待并降级", timeout_sec)
        raise TimeoutError(f"接口调用超时（超过 {int(timeout_sec)} 秒未返回）")
    finally:
        executor.shutdown(wait=False)