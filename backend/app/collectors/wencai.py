"""pywencai 问财接入（T6.4，开发文档 §2.3.3 避坑4）：条件选股主源 + 研报主源。

风险与对策（§2.3.3）：
- cookie 过期 → 空结果/403：cookie 走接口配置 params_template {"cookie": "..."}
  （设置页可改），失效抛 RuntimeError 带修复提示 → 采集链自动降级 + 告警
- 问财前端改版解析挂 → 锁版本 pywencai==0.12.2（requirements-data.txt）
- 依赖 Node.js ≥ 18 → fetch 前校验 node 可执行，缺失给出明确错误
- 降级线：akshare-东财研报（collectors.research）+ 本地缓存兜底，采集链按优先级自动切换
"""
import shutil

from app.collectors.base import BaseCollector


def _to_records(df) -> list[dict]:
    return df.to_dict("records") if hasattr(df, "to_dict") else list(df or [])


def _require_node() -> None:
    if shutil.which("node") is None:
        raise RuntimeError("pywencai 依赖 Node.js≥18：环境中未找到 node，问财线不可用")


def _fetch_wencai(query: str, cookie: str) -> list[dict]:
    _require_node()
    try:
        import pywencai
    except ImportError as e:
        raise RuntimeError(f"pywencai 未安装或版本不符（要求 ==0.12.2）: {e}") from e
    try:
        result = pywencai.get(query=query, cookie=cookie or None, loop=True)
    except AttributeError as e:
        # D-05 修复：问财接口层不可达/改版时，pywencai 对 None 取属性会抛
        # AttributeError（'NoneType' object has no attribute 'get'），
        # 与 cookie 失效无关，需给出可区分的排障提示。
        raise RuntimeError(
            "问财接口无返回（疑似接口改版或网络不可达，非 cookie 问题）："
            f"{e}；请检查到 x.iwencai.com 的连通性、或升级/锁定 pywencai 版本，"
            "必要时改用同数据类型的备源接口"
        ) from e
    except Exception as e:
        raise RuntimeError(f"问财请求失败（疑似 cookie 失效或风控）: {e}") from e
    if result is None:
        raise RuntimeError("问财返回空（疑似 cookie 失效，请在系统设置页-数据源接口配置更新 cookie）")
    if isinstance(result, str):
        raise RuntimeError("问财返回 HTML（接口改版/被拦截）：请锁版本/升级 pywencai 或更新 cookie")
    return _to_records(result)


class PywencaiCollector(BaseCollector):
    """问财条件选股（data_type=stock_select）。query/cookie 由接口配置 params_template 注入。"""

    data_type = "stock_select"

    def fetch(self, **params) -> list[dict]:
        query = str(params.get("query") or "").strip()
        if not query:
            raise RuntimeError("问财选股需要 query 参数（在接口配置 params_template 设置查询句）")
        return _fetch_wencai(query, str(params.get("cookie") or "").strip())


class PywencaiReportCollector(BaseCollector):
    """问财研报（data_type=research_report）。"""

    data_type = "research_report"

    def fetch(self, **params) -> list[dict]:
        query = str(params.get("query") or "").strip()
        if not query:
            raise RuntimeError("问财研报需要 query 参数（在接口配置 params_template 设置查询句）")
        return _fetch_wencai(query, str(params.get("cookie") or "").strip())
