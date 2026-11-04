"""数据源接口配置与状态（T3.5，开发文档 §7 /api/datasource/*、§2.4）。

- /interfaces：增删改 + 连通性测试（设置页"数据源接口配置"的数据面）
- /status：各数据源健康/切换状态（主源标识、最近成功、最近错误、24h 调用统计）
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.base import get_db
from app.models.system import DataSourceConfig, DataSourceLog
from app.services.source_registry import (
    create_source,
    delete_source,
    pick_sources,
    probe_source,
    update_source,
)

router = APIRouter(prefix="/api/datasource", tags=["datasource"])


class InterfaceIn(BaseModel):
    data_type: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    impl_kind: str = Field("py_func", max_length=16)
    impl_ref: str = Field(min_length=1, max_length=128)
    params_template: dict = Field(default_factory=dict)
    role: str = Field("备用", max_length=8)
    priority: int = Field(10, ge=0, le=99)
    qps_limit: float = Field(1.0, gt=0, le=10)
    timeout_sec: int = Field(30, ge=1, le=300)
    enabled: bool = True


class InterfacePatch(BaseModel):
    """全部可选：只提交要改的项（含启停 enabled / 优先级 priority）。"""

    data_type: str | None = Field(None, max_length=32)
    name: str | None = Field(None, max_length=64)
    impl_kind: str | None = Field(None, max_length=16)
    impl_ref: str | None = Field(None, max_length=128)
    params_template: dict | None = None
    role: str | None = Field(None, max_length=8)
    priority: int | None = Field(None, ge=0, le=99)
    qps_limit: float | None = Field(None, gt=0, le=10)
    timeout_sec: int | None = Field(None, ge=1, le=300)
    enabled: bool | None = None


def _view(row: DataSourceConfig) -> dict:
    return {
        "id": row.id, "data_type": row.data_type, "name": row.name,
        "impl_kind": row.impl_kind, "impl_ref": row.impl_ref,
        "params_template": row.params_template or {},
        "role": row.role, "priority": row.priority,
        "qps_limit": row.qps_limit, "timeout_sec": row.timeout_sec,
        "enabled": row.enabled,
        "last_ok_at": row.last_ok_at.isoformat() if row.last_ok_at else None,
        "last_error": row.last_error or "",
    }


@router.get("/interfaces")
def interfaces_list(
    data_type: str | None = Query(None, description="按数据类型过滤"),
    db: Session = Depends(get_db),
):
    q = db.query(DataSourceConfig)
    if data_type:
        q = q.filter(DataSourceConfig.data_type == data_type)
    rows = q.order_by(DataSourceConfig.data_type.asc(), DataSourceConfig.priority.asc()).all()
    return {"items": [_view(r) for r in rows], "total": len(rows)}


@router.post("/interfaces")
def interfaces_create(payload: InterfaceIn, db: Session = Depends(get_db)):
    dup = (db.query(DataSourceConfig)
           .filter(DataSourceConfig.data_type == payload.data_type,
                   DataSourceConfig.name == payload.name)
           .first())
    if dup is not None:
        raise AppError(409, "同数据类型下接口名已存在")
    src = create_source(db, **payload.model_dump())
    return _view(src)


@router.put("/interfaces/{source_id}")
def interfaces_update(source_id: int, payload: InterfacePatch, db: Session = Depends(get_db)):
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    src = update_source(db, source_id, **data)
    return _view(src)


@router.delete("/interfaces/{source_id}")
def interfaces_delete(source_id: int, db: Session = Depends(get_db)):
    delete_source(db, source_id)
    return {"ok": True}


@router.post("/interfaces/{source_id}/test")
def interfaces_test(source_id: int, db: Session = Depends(get_db)):
    """连通性测试：试采 1 次，返回结果与耗时（设置页"测试"按钮）。"""
    return probe_source(db, source_id)


@router.get("/status")
def datasource_status(db: Session = Depends(get_db)):
    """各数据源健康/切换状态：当前选路（主源）+ 最近成功/错误 + 24h 调用统计。"""
    rows = (db.query(DataSourceConfig)
            .order_by(DataSourceConfig.data_type.asc(), DataSourceConfig.priority.asc())
            .all())
    active: dict[str, int] = {}
    for src in rows:  # 同类型第一个启用的即当前选路
        if src.enabled and src.data_type not in active:
            active[src.data_type] = src.id

    since = datetime.now() - timedelta(hours=24)
    stats = {
        (t, s): n
        for t, s, n in (
            db.query(DataSourceLog.data_type, DataSourceLog.status, func.count())
            .filter(DataSourceLog.created_at >= since)
            .group_by(DataSourceLog.data_type, DataSourceLog.status)
            .all()
        )
    }
    items = []
    for r in rows:
        item = _view(r)
        item["is_active"] = active.get(r.data_type) == r.id
        item["ok_24h"] = stats.get((r.data_type, "ok"), 0) if item["is_active"] else 0
        item["fail_24h"] = sum(
            n for (t, s), n in stats.items() if t == r.data_type and s == "fail"
        )
        items.append(item)
    return {"items": items, "total": len(items), "updated_at": datetime.now().isoformat()}
