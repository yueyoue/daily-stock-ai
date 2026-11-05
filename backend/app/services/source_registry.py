"""数据源接口配置与选路（开发文档 §2.4 核心设计）。

- 采集器不硬编码接口：按「数据类型 → 启用中优先级最高」取接口，失败由采集链降级
- impl_ref 约定："模块:类"（实例化后有 fetch(**params)）或 "模块:函数"（直接调用）
- CRUD 供系统设置页（T3.5 API / T4.8 UI）；每次调用写 data_source_log

⚠️ 缺陷 D-03 修复：接口配置的 timeout_sec 真实施加到取数调用上
（call_source / probe_source 统一走 call_with_timeout），超时抛 TimeoutError → 采集链降级。
"""
import importlib
import logging
import time
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.system import DataSourceConfig, DataSourceLog
from app.utils.ratelimit import RateLimiter
from app.utils.timeout import call_with_timeout

logger = logging.getLogger("app.source_registry")

_rate_limiter = RateLimiter()

# 可编辑字段白名单（设置页增删改用）；data_type 创建后也可改（调用方自行保证一致性）
_EDITABLE_FIELDS = {
    "data_type", "name", "impl_kind", "impl_ref", "params_template", "role",
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
    try:
        db.commit()
    except Exception:
        # 会话已在 pending-rollback 态时也要能落日志（D-01 同类兜底）
        db.rollback()
        logger.warning("数据源日志写入失败（已回滚忽略）")


def call_source(db: Session, source: DataSourceConfig, **params) -> list[dict]:
    """调用单接口：QPS 限流 → 采集 → 记日志/更新健康状态。失败抛 DataSourceError。

    params_template 作为默认参数合并（调用方参数优先）——接口行为差异
    （如 stock_info 主源带概念/备源轻量）由配置控制，不写死在调用方。
    timeout_sec 作为取数截止时间真实生效（D-03）。
    """
    _rate_limiter.wait(f"src:{source.id}", source.qps_limit)
    t0 = time.monotonic()
    merged = {**(source.params_template or {}), **params}
    try:
        collector = build_collector(source)
        records = call_with_timeout(lambda: collector.fetch(**merged) or [],
                                    source.timeout_sec) or []
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


def probe_source(db: Session, source_id: int, params: dict | None = None) -> dict:
    """连通性测试：试采 1 次，返回结果供设置页展示。

    - 参数 = 接口自带 params_template 合并调用方传入的样例参数（调用方优先）。
      D-08 修复：kline / finance / research_report 等"需 code/query 参数"的数据源
      仅靠 params_template 无法试采，允许在测试请求里带样例参数完成试采。
    - ⚠️ 命名注意：服务函数禁止用 test_ 前缀（pytest 会误收集），故名 probe_source。
    - timeout_sec 作为取数截止时间真实生效（D-03），避免页面无限等待。
    """
    src = db.get(DataSourceConfig, source_id)
    if src is None:
        raise AppError(404, "接口配置不存在")
    merged = {**(src.params_template or {}), **(params or {})}
    t0 = time.monotonic()
    try:
        collector = build_collector(src)
        records = call_with_timeout(lambda: collector.fetch(**merged) or [],
                                    src.timeout_sec) or []
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