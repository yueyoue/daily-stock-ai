"""指标缓存批处理任务（T2.3，开发文档 §5.3/§4.9 16:00 指标批量计算）。

任务本体 + task_log 全程记录；APScheduler 注册（交易日 16:00，TZ=Asia/Shanghai）
随 T7.5 定时调度统一接线，届时调度链路为：15:30 收盘采集 → 16:00 本任务。
"""
import logging
import time
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from app.services.indicator_cache import build_daily_cache, cleanup_cache
from app.services.pipeline import log_stage

logger = logging.getLogger("app.indicator_jobs")


def job_build_indicator_cache(db: Session, run_date: date | None = None,
                              keep_days: int | None = None,
                              engine: str | None = None,
                              cache_dir: Path | None = None) -> dict:
    """构建当日指标缓存并滚动清理旧版（task_log 全程记录）。"""
    run_date = run_date or date.today()
    top = log_stage(db, "指标缓存批处理", "running", message=f"date={run_date}")
    t0 = time.monotonic()
    try:
        meta = build_daily_cache(db, run_date, engine=engine, cache_dir=cache_dir)
        deleted = cleanup_cache(cache_dir=cache_dir, keep_days=keep_days)
        duration_ms = int((time.monotonic() - t0) * 1000)
        top.status = "success"
        top.duration_ms = duration_ms
        top.message = (f"date={run_date} rows={meta['rows']} skipped={meta['skipped']} "
                       f"engine={meta['engine']} 清理 {len(deleted)} 个旧版")
        db.commit()
        return {**meta, "deleted": deleted, "duration_ms": duration_ms}
    except Exception as e:
        top.status = "failed"
        top.duration_ms = int((time.monotonic() - t0) * 1000)
        top.message = f"date={run_date} 失败: {e}"[:2000]
        db.commit()
        logger.exception("指标缓存批处理失败")
        raise
