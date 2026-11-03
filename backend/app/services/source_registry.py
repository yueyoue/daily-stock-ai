"""数据源接口配置与选路（开发文档 §2.4 核心设计）。

- 采集器不硬编码接口：按「数据类型 → 启用中优先级最高」取接口，失败由采集链降级
- impl_ref 约定："模块:类"（实例化后有 fetch(**params)）或 "模块:函数"（直接调用）
- CRUD 供系统设置页（T3.5 API / T4.8 UI）；每次调用写 data_source_log
"""
import importlib
import logging
import time
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.system import DataSourceConfig, DataSourceLog
from app.utils.ratelimit import RateLimiter

logger = logging.getLogger("app.source_registry")

_rate_limiter = RateLimiter()

# 可编辑字段白名单（设置页增删改用）
_EDITABLE_FIELDS = {
    "name", "impl_kind", "impl_ref", "params_template", "role",
    "priority", "qps_limit", "timeout_sec", "enabled",
}


class DataSourceError(Exception):
    """单接口采集失败（采集链捕获后自动降级到下一优先级）。"""

    def __init__(self, source_name: str, message: str):
        self.source_name = source_name
        super().__init__(message)


# ---------- 选路 ----------
def pick_sources(db: Session, data_type: str) -> list[DataSourceConfig]:
    return (
        db.query(DataSourceConfig)
        .filter(
            DataSourceConfig.data_type == data_type,
            DataSourceConfig.enabled.is_(True),
        )
        .order_by(DataSourceConfig.priority.asc())
        .all()
    )


def build_collector(source: DataSourceConfig):
    """由 impl_ref 解析并实例化采集器。"""
    if source.impl_kind != "py_func":
        raise DataSourceError(source.name, f"暂不支持的实现方式: {source.impl_kind}")
    mod_name, _, attr = source.impl_ref.partition(":")
    if not attr:
        raise DataSourceError(source.name, f"impl_ref 格式错误（应为 模块:类/函数）: {source.impl_ref}")
    try:
        module = importlib.import_module(mod_name)
        obj = getattr(module, attr)
    except (ImportError, AttributeError) as e:
        raise DataSourceError(source.name, f"加载实现失败: {e}")
    return obj() if isinstance(obj, type) else obj


def write_log(
    db: Session,
    source: DataSourceConfig,
    status: str,
    duration_ms: int,
    message: str = "",
) -> None:
    db.add(
        DataSourceLog(
            data_type=source.data_type,
            source_name=source.name,
            status=status,
            duration_ms=duration_ms,
            message=message[:500],
        )
    )
    db.commit()


def call_source(db: Session, source: DataSourceConfig, **params) -> list[dict]:
    """调用单接口：QPS 限流 → 采集 → 记日志/更新健康状态。失败抛 DataSourceError。"""
    _rate_limiter.wait(f"src:{source.id}", source.qps_limit)
    t0 = time.monotonic()
    try:
        collector = build_collector(source)
        records = collector.fetch(**params) or []
        duration_ms = int((time.monotonic() - t0) * 1000)
        source.last_ok_at = datetime.now()
        source.last_error = ""
        write_log(db, source, "ok", duration_ms, f"{len(records)} 条")
        return records
    except DataSourceError as e:
        duration_ms = int((time.monotonic() - t0) * 1000)
        source.last_error = str(e)[:256]
        write_log(db, source, "fail", duration_ms, str(e))
        raise
    except Exception as e:
        duration_ms = int((time.monotonic() - t0) * 1000)
        source.last_error = str(e)[:256]
        write_log(db, source, "fail", duration_ms, str(e))
        raise DataSourceError(source.name, str(e))


# ---------- CRUD（设置页：增删改/启停/优先级）----------
def create_source(db: Session, **fields) -> DataSourceConfig:
    data = {k: v for k, v in fields.items() if k in _EDITABLE_FIELDS}
    for key in ("data_type", "name", "impl_ref"):
        if not data.get(key):
            raise AppError(400, f"缺少必填字段: {key}")
    src = DataSourceConfig(**data)
    db.add(src)
    db.commit()
    return src


def update_source(db: Session, source_id: int, **fields) -> DataSourceConfig:
    src = db.get(DataSourceConfig, source_id)
    if src is None:
        raise AppError(404, "接口配置不存在")
    for k, v in fields.items():
        if k in _EDITABLE_FIELDS:
            setattr(src, k, v)
    db.commit()
    return src


def delete_source(db: Session, source_id: int) -> None:
    src = db.get(DataSourceConfig, source_id)
    if src is None:
        raise AppError(404, "接口配置不存在")
    db.delete(src)
    db.commit()


def test_connection(db: Session, source_id: int) -> dict:
    """连通性测试：试采 1 次（用接口自带参数模板），返回结果供设置页展示。"""
    src = db.get(DataSourceConfig, source_id)
    if src is None:
        raise AppError(404, "接口配置不存在")
    t0 = time.monotonic()
    try:
        collector = build_collector(src)
        records = collector.fetch(**(src.params_template or {})) or []
        duration_ms = int((time.monotonic() - t0) * 1000)
        src.last_ok_at = datetime.now()
        src.last_error = ""
        write_log(db, src, "ok", duration_ms, f"连通测试 {len(records)} 条")
        return {"ok": True, "count": len(records), "duration_ms": duration_ms}
    except Exception as e:
        duration_ms = int((time.monotonic() - t0) * 1000)
        src.last_error = str(e)[:256]
        write_log(db, src, "fail", duration_ms, f"连通测试失败: {e}")
        return {"ok": False, "count": 0, "duration_ms": duration_ms, "error": str(e)}
