"""系统配置接口（T3.5，开发文档 §7：GET /api/config/get、POST /api/config/save）。"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.models.base import get_db
from app.services.config_store import get_config, save_config
from app.services.llm import probe_llm

router = APIRouter(prefix="/api/config", tags=["config"])


class ConfigUpdate(BaseModel):
    """全部可选：只提交要改的项。llm_api_key 只进不出，空值视为不修改。

    D-07 修复：extra="forbid" —— 提交未知配置项直接报参数校验错误（400），
    不再被 pydantic 静默丢弃后变成"空操作"却返回 200。
    """

    model_config = ConfigDict(extra="forbid")

    tech_indicator_store: bool | None = None
    indicator_cache_days: int | None = None
    indicator_engine: str | None = None
    report_am_cron: str | None = None
    report_pm_cron: str | None = None
    # T7.5 定时调度：采集线时间 + 四类任务开关
    collect_am_cron: str | None = None
    collect_pm_cron: str | None = None
    collect_am_enabled: bool | None = None
    collect_pm_enabled: bool | None = None
    report_am_enabled: bool | None = None
    report_pm_enabled: bool | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    # T7.6 失败告警 webhook（空串 = 关闭，改后下次任务失败即生效）
    alert_webhook: str | None = None


@router.get("/get")
def config_get(db: Session = Depends(get_db)):
    return get_config(db)


@router.post("/save")
def config_save(payload: ConfigUpdate, db: Session = Depends(get_db)):
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    return save_config(db, **data)


@router.post("/test_llm")
def config_test_llm(db: Session = Depends(get_db)):
    """LLM 连通测试（T4.9）：最小 completion 调用，响应不含 Key 明文。"""
    return probe_llm(db)