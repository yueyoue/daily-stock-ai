"""报告服务（T3.2 骨架；T7.4 接 LLM 生成流水线）。

- list_reports / get_report：历史报告查询（分页，T3.1 规范）
- regenerate_report：重新生成指定日期报告——当前落**结构化骨架**（把 market_daily
  等结构化数据填进 7 维度/复盘要素模板），LLM 生成 T7.4 接入后替换正文生成逻辑；
  重复生成覆盖旧记录并 version+1 留版本痕迹
"""
from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.content import Report
from app.models.market import MarketDaily
from app.schemas.common import Pagination

REPORT_TYPES = ("morning", "afternoon")


def list_reports(db: Session, page: Pagination, type: str | None = None) -> dict:
    q = db.query(Report)
    if type:
        q = q.filter(Report.type == type)
    total = q.count()
    rows = (
        q.order_by(Report.report_date.desc(), Report.id.desc())
        .offset(page.offset).limit(page.limit)
        .all()
    )
    return {
        "items": [
            {"id": r.id, "type": r.type, "report_date": r.report_date.isoformat(),
             "title": r.title, "generated_at": r.generated_at.isoformat()
             if r.generated_at else None, "version": r.version}
            for r in rows
        ],
        "total": total, "page": page.page, "page_size": page.page_size,
    }


def get_report(db: Session, report_id: int) -> Report | None:
    return db.get(Report, report_id)


# ---------- 骨架生成（T7.4 换 LLM 正文） ----------

def _market_facts(db: Session, d: date) -> dict:
    row = db.get(MarketDaily, d)
    if row is None:
        return {}
    extra = row.extra or {}
    return {
        "情绪得分": row.sentiment_score,
        "涨停家数": row.limit_up_count,
        "跌停家数": row.limit_down_count,
        "最高连板": extra.get("最高连板"),
        "连板梯队": extra.get("连板梯队"),
        "主力资金": row.main_moneyflow,
        "上涨家数": row.up_count,
        "下跌家数": row.down_count,
    }


def _morning_skeleton(d: date, facts: dict) -> str:
    data = "；".join(f"{k}={v}" for k, v in facts.items() if v is not None) or "（待采集）"
    return f"""# {d.isoformat()} 盘前早报（结构化骨架，LLM 生成 T7.4 接入）

## 1. 速读摘要
（待生成）

## 2. 隔夜市场大事
（待生成）

## 3. 市场情绪预判
结构化输入：{data}

## 4. 资金动向
主力资金（东财口径）：{facts.get('主力资金', '（待采集）')}；北向净流入为季度披露不可实时取（§10.7）。

## 5. 操作提醒 / 风险提示
（待生成）

## 6. 今日观察方向
（待生成）

## 7. 今日重要事件与数据日历
（待生成）
"""


def _afternoon_skeleton(d: date, facts: dict) -> str:
    ladder = facts.get("连板梯队") or {}
    ladder_txt = "，".join(f"{k} {v} 家" for k, v in ladder.items()) or "（待采集）"
    return f"""# {d.isoformat()} 盘后复盘（结构化骨架，LLM 生成 T7.4 接入）

- 市场情绪得分：{facts.get('情绪得分', '（待采集）')}
- 涨停/跌停：{facts.get('涨停家数', '—')} / {facts.get('跌停家数', '—')}
- 涨停梯队：{ladder_txt}（最高 {facts.get('最高连板', '—')} 板）
- 主线板块：（待生成）
- 资金流向：主力资金 {facts.get('主力资金', '（待采集）')}
- 风险预警：（待生成）
- 次日观察计划：（待生成）
- 策略成功率验证：（待 T5.x 信号接入后回填）
"""


def regenerate_report(db: Session, report_type: str, report_date: date) -> Report:
    if report_type not in REPORT_TYPES:
        raise AppError(400, f"type 需为 {REPORT_TYPES} 之一")
    facts = _market_facts(db, report_date)
    md = (_morning_skeleton if report_type == "morning"
          else _afternoon_skeleton)(report_date, facts)
    title = f"{report_date.isoformat()} {'盘前早报' if report_type == 'morning' else '盘后复盘'}"

    row = (
        db.query(Report)
        .filter(Report.type == report_type, Report.report_date == report_date)
        .order_by(Report.id.desc())
        .first()
    )
    if row is None:
        row = Report(type=report_type, report_date=report_date,
                     title=title, md_content=md, version=1)
        db.add(row)
    else:  # 覆盖旧记录并留版本痕迹（T7.4 验收口径）
        row.title = title
        row.md_content = md
        row.version = (row.version or 1) + 1
    db.commit()
    db.refresh(row)
    return row
