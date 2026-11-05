"""报告接口（T3.2 查询 + T7.4 LLM 生成流水线，开发文档 §7 /api/report/*）。

- list / detail：历史报告查询
- regenerate：数据 → LLM → Markdown → 校验重试 → 入库（覆盖 + version+1）；
  响应带 ok/attempts/fallback/fallback_reason/problems，便于前端提示生成质量与降级原因
"""
from datetime import date

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.base import get_db
from app.schemas.common import Pagination
from app.api.deps import get_pagination
from app.services.report import REPORT_TYPES, generate_and_store, get_report, list_reports

router = APIRouter(prefix="/api/report", tags=["report"])


class ReportItem(BaseModel):
    id: int
    type: str
    report_date: str
    title: str
    generated_at: str | None = None
    version: int


class ReportPage(BaseModel):
    items: list[ReportItem]
    total: int
    page: int
    page_size: int


class ReportDetail(ReportItem):
    md_content: str


class RegenerateRequest(BaseModel):
    type: str  # morning / afternoon
    report_date: date


class RegenerateResponse(BaseModel):
    id: int
    type: str
    report_date: str
    version: int
    generated_at: str | None = None
    ok: bool = True
    attempts: int = 0
    fallback: bool = False
    # D-14 修复：降级（LLM 未就绪落结构化骨架）时回传具体原因，前端可如实提示
    fallback_reason: str = ""
    llm_used: bool = False
    problems: list[str] = []


@router.get("/list", response_model=ReportPage)
def report_list(
    type: str | None = Query(None, description="morning / afternoon，空为全部"),
    page: Pagination = Depends(get_pagination),
    db: Session = Depends(get_db),
):
    if type and type not in REPORT_TYPES:
        raise AppError(400, f"type 需为 {REPORT_TYPES} 之一")
    return list_reports(db, page, type=type)


@router.get("/{report_id}", response_model=ReportDetail)
def report_detail(report_id: int, db: Session = Depends(get_db)):
    row = get_report(db, report_id)
    if row is None:
        raise AppError(404, "报告不存在")
    return {
        "id": row.id, "type": row.type, "report_date": row.report_date.isoformat(),
        "title": row.title, "md_content": row.md_content,
        "generated_at": row.generated_at.isoformat() if row.generated_at else None,
        "version": row.version,
    }


@router.post("/regenerate", response_model=RegenerateResponse)
def report_regenerate(payload: RegenerateRequest, db: Session = Depends(get_db)):
    """重新生成指定日期报告（覆盖旧记录 version+1）。"""
    res = generate_and_store(db, payload.type, payload.report_date)
    row = res["report"]
    return {
        "id": row.id, "type": row.type, "report_date": row.report_date.isoformat(),
        "version": row.version,
        "generated_at": row.generated_at.isoformat() if row.generated_at else None,
        "ok": res["ok"], "attempts": res["attempts"], "fallback": res["fallback"],
        "fallback_reason": res.get("fallback_reason") or "",
        "llm_used": res["llm_used"], "problems": res["problems"],
    }