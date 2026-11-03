"""采集链（开发文档 §4 模块1 / §2.4.2）：主备自动切换 + 全挂告警。

流程：按优先级取启用接口 → 逐个调用（限流/日志/健康状态在 call_source）→
失败自动降级下一优先级 → 全部失败抛 502 并汇总原因。
"""
import logging

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.services.source_registry import DataSourceError, call_source, pick_sources

logger = logging.getLogger("app.collect_chain")


def collect_with_fallback(db: Session, data_type: str, **params) -> list[dict]:
    """采集指定数据类型：主源失败自动降级，返回首个成功源的数据。"""
    sources = pick_sources(db, data_type)
    if not sources:
        raise AppError(502, f"数据类型 {data_type} 无可用接口配置（请在系统设置页添加/启用）")

    errors: list[str] = []
    for src in sources:
        try:
            records = call_source(db, src, **params)
            logger.info("[%s] %s 采集成功 %d 条", data_type, src.name, len(records))
            return records
        except DataSourceError as e:
            logger.warning("[%s] %s 失败，降级下一优先级：%s", data_type, e.source_name, e)
            errors.append(f"{e.source_name}: {e}")

    # 全挂：告警（task_log 由调用方记）+ 统一错误
    raise AppError(502, f"{data_type} 全部接口失败（{len(errors)} 个）: " + "; ".join(errors))
