"""采集器插件规范（开发文档 §4 模块1）。

所有采集器实现本基类；返回值为**标准化记录列表**（list[dict]），字段约定：
- kline: code/date/open/high/low/close/volume/amount/adj_factor
- 其余数据类型字段见开发文档 §6 对应表；多余字段进 extra/JSON

实现类通过 data_source_config.impl_ref（"模块:类"）注册，采集链动态加载；
禁止在业务代码里直接 import 具体数据源（§2.4 硬约束）。

⚠️ 缺陷 D-03 修复：连通性测试默认实现接受 timeout_sec，经 call_with_timeout 施加
截止时间（避免接口挂起时页面无限等待）。
"""
from abc import ABC, abstractmethod

from app.utils.timeout import call_with_timeout


class BaseCollector(ABC):
    """单接口采集器。fetch 只负责取数与标准化，失败抛异常即可（降级由采集链处理）。"""

    data_type: str = ""

    @abstractmethod
    def fetch(self, **params) -> list[dict]:
        """采集并返回标准化记录。params 来自接口配置的 params_template + 调用方参数。"""

    def test_connection(self, timeout_sec: float | None = None, **params) -> bool:
        """连通性测试默认实现：试采一条（timeout_sec 生效，避免接口挂起）。"""
        try:
            return bool(call_with_timeout(lambda: self.fetch(**params), timeout_sec))
        except Exception:
            return False