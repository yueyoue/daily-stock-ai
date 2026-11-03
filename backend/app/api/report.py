"""报告接口（T3.2，开发文档 §7 /api/report/*；LLM 生成 T7.4 接入）。"""
from datetime import date

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.base import get_db
from app.schemas.common import Pagination
from app.api.deps import get_pagination
from app.services.report import REPORT_TYPES, get_report, list_reports, regenerate_report

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
    row = regenerate_report(db, payload.type, payload.report_date)
    return {
        "id": row.id, "type": row.type, "report_date": row.report_date.isoformat(),
        "version": row.version,
        "generated_at": row.generated_at.isoformat() if row.generated_at else None,
    }
