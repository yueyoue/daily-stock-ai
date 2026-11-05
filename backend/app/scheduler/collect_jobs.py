"""定时采集任务本体（T7.5，开发文档 §4.9：07:30 隔夜资讯 / 15:30 收盘采集）。

- job_collect_overnight：资讯线（财联社电报主源 / 同花顺快讯备用），主源失败自动降级。
  资讯采集链 collect_with_fallback 只返回 records、不返回来源名，无法标注 source，
  故此处显式 pick_sources + call_source，用 src.name 做归一化来源标记。
- job_collect_close：收盘数据采集（行情/资金/涨停）= 复用采集流水线 incremental
  （stock_info + kline 尾部增量 + market_daily：moneyflow/limit_updown/market_summary），
  task_log 由流水线全程记录。
- 交易日前置判断在调度层（registry.run_job），本层不判断。
"""
import logging
import time
from datetime import date

from sqlalchemy.orm import Session

from app.collectors.news import normalize_batch
from app.core.errors import AppError
from app.services.news import ingest_news
from app.services.pipeline import log_stage, run_pipeline
from app.services.source_registry import DataSourceError, call_source, pick_sources

logger = logging.getLogger("app.scheduler.collect_jobs")


def job_collect_close(db: Session, run_date: date | None = None) -> dict:
    """交易日 15:30 收盘数据采集（行情/资金/涨停）。"""
    run_date = run_date or date.today()
    return run_pipeline(db, "incremental", run_date=run_date)


def job_collect_overnight(db: Session, run_date: date | None = None) -> dict:
    """每日 07:30 隔夜资讯采集（为 08:00 早报供料；非交易日仍抓资讯，§4.9 注）。"""
    run_date = run_date or date.today()
    top = log_stage(db, "隔夜资讯采集", "running", message=f"date={run_date.isoformat()}")
    t0 = time.monotonic()

    sources = pick_sources(db, "news")
    if not sources:
        top.status = "failed"
        top.duration_ms = 0
        top.message = "news 无可用接口配置（请在系统设置页添加/启用）"
        db.commit()
        raise AppError(502, "news 无可用接口配置（请在系统设置页添加/启用）")

    errors: list[str] = []
    for src in sources:
        try:
            rows = call_source(db, src)
        except DataSourceError as e:
            errors.append(f"{e.source_name}: {e}")
            logger.warning("[news] %s 失败，降级下一优先级：%s", e.source_name, e)
            continue
        records = normalize_batch(rows, src.name)
        stats = ingest_news(db, records)
        duration_ms = int((time.monotonic() - t0) * 1000)
        top.status = "success"
        top.duration_ms = duration_ms
        top.message = (
            f"date={run_date.isoformat()} 源={src.name} 采集={len(rows)} "
            f"入库={stats['inserted']} 重复={stats['duplicates']} 跳过={stats['skipped']}"
        )
        db.commit()
        return {"date": run_date.isoformat(), "source": src.name, "fetched": len(rows),
                "ingested": stats, "duration_ms": duration_ms, "task_id": top.id}

    duration_ms = int((time.monotonic() - t0) * 1000)
    top.status = "failed"
    top.duration_ms = duration_ms
    top.message = (f"date={run_date.isoformat()} 全部接口失败（{len(errors)} 个）: "
                   + "; ".join(errors))[:2000]
    db.commit()
    raise AppError(502, f"隔夜资讯全部接口失败（{len(errors)} 个）: " + "; ".join(errors))