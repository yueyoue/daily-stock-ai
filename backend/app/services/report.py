"""报告服务（T3.2 查询 + T7.4 生成流水线）。

- list_reports / get_report：历史报告查询（分页，T3.1 规范）
- generate_and_store：报告生成流水线（T7.4）——结构化输入 → LLM → Markdown →
  维度与关键数值校验（不合格带问题清单自动重试）→ 写入 report 表；
  重复生成覆盖旧记录并 version+1 + 刷新 generated_at，留版本痕迹
- 降级口径：LLM 未就绪（未配置 Key / 依赖缺失，503）时落**结构化骨架**并在
  task_log 留 warning，保证无 Key 环境接口仍可用；巡检把骨架正文换成真实值即可
- job_generate_morning / job_generate_afternoon：定时任务入口（T7.5 接 registry；
  交易日历前置判断在 T7.5 调度层，本层不判断）
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.content import Report
from app.models.market import MarketDaily
from app.schemas.common import Pagination
from app.services.pipeline import log_stage

logger = logging.getLogger("app.report")

REPORT_TYPES = ("morning", "afternoon")
REPORT_TITLES: dict[str, str] = {"morning": "盘前早报", "afternoon": "盘后复盘"}

# LLM 未就绪的错误码（未配置 Key / 依赖缺失）：降级骨架，而非整体失败
LLM_UNAVAILABLE_CODES = (503,)

FALLBACK_NOTE = "（结构化骨架·LLM 未就绪降级输出）"
FALLBACK_PLACEHOLDER = "（LLM 未就绪，配置 Key 后重新生成该报告）"


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


# ---------- 结构化骨架（LLM 未就绪时的降级输出，字段口径与 LLM 正文一致） ----------

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
    return f"""# {d.isoformat()} 盘前早报{FALLBACK_NOTE}

## 1. 速读摘要
{FALLBACK_PLACEHOLDER}

## 2. 隔夜市场大事
{FALLBACK_PLACEHOLDER}

## 3. 市场情绪预判
结构化输入：{data}

## 4. 资金动向
主力资金（东财口径）：{facts.get('主力资金', '（待采集）')}；北向净流入为季度披露不可实时取（§10.7）。

## 5. 操作提醒 / 风险提示
{FALLBACK_PLACEHOLDER}

## 6. 今日观察方向
{FALLBACK_PLACEHOLDER}

## 7. 今日重要事件与数据日历
{FALLBACK_PLACEHOLDER}
"""


def _afternoon_skeleton(d: date, facts: dict) -> str:
    ladder = facts.get("连板梯队") or {}
    ladder_txt = "，".join(f"{k} {v} 家" for k, v in ladder.items()) or "（待采集）"
    return f"""# {d.isoformat()} 盘后复盘{FALLBACK_NOTE}

- 市场情绪打分（规则化口径）：市场情绪得分 {facts.get('情绪得分', '（待采集）')}
- 涨停/跌停：{facts.get('涨停家数', '—')} / {facts.get('跌停家数', '—')}
- 涨停梯队：{ladder_txt}（最高 {facts.get('最高连板', '—')} 板）
- 主线板块：{FALLBACK_PLACEHOLDER}
- 资金流向：主力资金 {facts.get('主力资金', '（待采集）')}
- 风险预警：{FALLBACK_PLACEHOLDER}
- 次日观察计划：{FALLBACK_PLACEHOLDER}
- 策略成功率验证：{FALLBACK_PLACEHOLDER}
"""


def _skeleton(db: Session, report_type: str, d: date) -> str:
    """降级骨架正文；复盘额外把规则化情绪分写回库（T7.3 口径的唯一生产者）。"""
    if report_type == "afternoon":
        from app.services.sentiment import apply_sentiment_score

        apply_sentiment_score(db, d)
    facts = _market_facts(db, d)
    return (_morning_skeleton if report_type == "morning"
            else _afternoon_skeleton)(d, facts)


# ---------- 生成流水线（T7.4） ----------

def _run_generator(db: Session, report_type: str, d: date, llm, max_attempts: int) -> dict:
    """调用模板层生成器（延迟导入，便于测试注入 llm）。"""
    if report_type == "morning":
        from app.services.report_morning import generate_morning as gen
    else:
        from app.services.report_afternoon import generate_afternoon as gen
    return gen(db, d, llm=llm, max_attempts=max_attempts)


def generate_and_store(
    db: Session,
    report_type: str,
    report_date: date,
    *,
    llm=None,
    max_attempts: int = 2,
    allow_skeleton_fallback: bool = True,
) -> dict:
    """报告生成流水线：结构化输入 → LLM → Markdown → 校验重试 → 入库（覆盖 + version+1）。

    返回 {report, report_type, report_date, ok, attempts, problems, fallback,
          fallback_reason, llm_used, chars, task_name}。

    - 维度/数值校验不合格：模板层已带问题清单重试 max_attempts 次，仍不合格则抛
      502（错误信息含问题清单），不写库、不覆盖旧报告
    - LLM 未就绪（503）且 allow_skeleton_fallback=True：落结构化骨架（task_log warning）
    - 入库：同类型同日期已有记录则覆盖正文并 version+1、刷新 generated_at
    """
    if report_type not in REPORT_TYPES:
        raise AppError(400, f"type 需为 {REPORT_TYPES} 之一")

    task_name = f"{REPORT_TITLES[report_type]}生成"
    fallback = False
    fallback_reason = ""
    attempts = 0
    problems: list[str] = []
    result: dict | None = None

    try:
        result = _run_generator(db, report_type, report_date, llm, max_attempts)
    except AppError as exc:
        if allow_skeleton_fallback and exc.code in LLM_UNAVAILABLE_CODES:
            fallback, fallback_reason = True, exc.message
            logger.warning("%s LLM 未就绪，降级结构化骨架：%s", task_name, fallback_reason)
            log_stage(db, task_name, "warning",
                      message=(f"date={report_date.isoformat()} 降级结构化骨架："
                               f"{fallback_reason}"))
        else:
            raise

    if fallback:
        md = _skeleton(db, report_type, report_date)
    else:
        assert result is not None
        md = result["md"]
        attempts = int(result["attempts"])
        problems = list(result["problems"])
        if not result["ok"]:
            raise AppError(
                502,
                f"{task_name}不合格（已自动重试 {attempts} 次）：" + "；".join(problems),
            )

    title = f"{report_date.isoformat()} {REPORT_TITLES[report_type]}"
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
        row.generated_at = datetime.now()
    db.commit()
    db.refresh(row)

    return {
        "report": row,
        "report_type": report_type,
        "report_date": report_date,
        "ok": True,
        "attempts": attempts,
        "problems": problems,
        "fallback": fallback,
        "fallback_reason": fallback_reason,
        "llm_used": not fallback,
        "chars": len(md),
        "task_name": task_name,
    }


# ---------- 定时任务入口（T7.5 接 registry） ----------

def job_generate_morning(db: Session, run_date: date | None = None) -> dict:
    """盘前早报任务本体（T7.5 接线；交易日历前置判断在调度层）。"""
    return generate_and_store(db, "morning", run_date or date.today())


def job_generate_afternoon(db: Session, run_date: date | None = None) -> dict:
    """盘后复盘任务本体（T7.5 接线；交易日历前置判断在调度层）。"""
    return generate_and_store(db, "afternoon", run_date or date.today())


__all__ = [
    "REPORT_TYPES", "REPORT_TITLES", "FALLBACK_NOTE", "LLM_UNAVAILABLE_CODES",
    "generate_and_store", "get_report", "job_generate_afternoon",
    "job_generate_morning", "list_reports",
]