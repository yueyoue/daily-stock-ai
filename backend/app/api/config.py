"""系统配置接口（T3.5，开发文档 §7：GET /api/config/get、POST /api/config/save）。"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models.base import get_db
from app.services.config_store import get_config, save_config
from app.services.llm import probe_llm

router = APIRouter(prefix="/api/config", tags=["config"])


class ConfigUpdate(BaseModel):
    """全部可选：只提交要改的项。llm_api_key 只进不出，空值视为不修改。"""

    tech_indicator_store: bool | None = None
    indicator_cache_days: int | None = None
    indicator_engine: str | None = None
    report_am_cron: str | None = None
    report_pm_cron: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None


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
