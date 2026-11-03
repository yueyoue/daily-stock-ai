"""模式 B 指标缓存管线（T2.3，开发文档 §5.3）。

收盘批处理：加载当日全市场 K 线 → 批量算 28 因子（双引擎，T2.2）→ 写 Parquet
（按交易日一版）→ 按交易日滚动保留 N 天（默认 3）清理旧版。
读缓存路径**全程不联网**（纯文件读取），供 T2.5 指标查询 / T5.2 选股扫描。

口径：
- 当日快照 = 当日有 K 线的标的（停牌/退市当日无行情，不入快照）；因子按该标的
  截至当日的全量历史计算（与"随查随出"口径一致）。
- 每版含元信息 meta JSON（日期/指标清单/行数/引擎/生成时间）。
- 单标的计算异常跳过并计数（护栏），不阻塞整批。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.market import StockKline
from app.services.tech_indicators import FLAT_KEYS, INDICATOR_SPEC, get_engine

logger = logging.getLogger("app.indicator_cache")

_PREFIX = "indicators_"


def _default_dir() -> Path:
    return Path(get_settings().indicator_cache_dir)


def _default_keep_days() -> int:
    return int(get_settings().indicator_cache_keep_days)


def _paths(cache_dir: Path, d: date) -> tuple[Path, Path]:
    stamp = d.strftime("%Y%m%d")
    return cache_dir / f"{_PREFIX}{stamp}.parquet", cache_dir / f"{_PREFIX}{stamp}.meta.json"


def list_cache_versions(cache_dir: Path | None = None) -> list[date]:
    """缓存版本（交易日）列表，新→旧。"""
    cache_dir = cache_dir or _default_dir()
    if not cache_dir.exists():
        return []
    out = []
    for p in cache_dir.glob(f"{_PREFIX}*.parquet"):
        stamp = p.stem[len(_PREFIX):]
        try:
            out.append(date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8])))
        except ValueError:
            continue
    return sorted(out, reverse=True)


def build_daily_cache(db: Session, trade_date: date | None = None,
                      engine: str | None = None,
                      cache_dir: Path | None = None) -> dict:
    """构建当日全市场指标快照（Parquet + meta）。返回统计。"""
    import pandas as pd

    trade_date = trade_date or date.today()
    cache_dir = cache_dir or _default_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    eng = get_engine(engine)

    codes = [r[0] for r in (
        db.query(StockKline.code)
        .filter(StockKline.date == trade_date)
        .distinct().all()
    )]
    rows: list[dict] = []
    skipped = 0
    t0 = time.monotonic()
    for code in codes:
        bars = (
            db.query(StockKline)
            .filter(StockKline.code == code, StockKline.date <= trade_date)
            .order_by(StockKline.date.asc())
            .all()
        )
        if not bars:
            continue
        try:
            vals = eng.compute_series(
                [b.close for b in bars], [b.high for b in bars],
                [b.low for b in bars], [b.volume for b in bars],
            )
        except Exception as e:  # 单标的失败不阻塞整批
            skipped += 1
            logger.warning("[%s] 指标计算失败跳过：%s", code, e)
            continue
        row = {"code": code, "date": trade_date.isoformat()}
        for k in FLAT_KEYS:
            row[k] = vals[k][-1] if vals[k] else None
        rows.append(row)

    parquet_path, meta_path = _paths(cache_dir, trade_date)
    frame = pd.DataFrame(rows, columns=["code", "date", *FLAT_KEYS])
    frame.to_parquet(parquet_path, index=False)
    meta = {
        "date": trade_date.isoformat(),
        "indicators": list(FLAT_KEYS),
        "spec_keys": [s["key"] for s in INDICATOR_SPEC],
        "rows": len(rows),
        "skipped": skipped,
        "engine": eng.name,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "duration_ms": int((time.monotonic() - t0) * 1000),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    logger.info("指标缓存 %s：写入 %d 行（跳过 %d，引擎 %s）",
                trade_date, len(rows), skipped, eng.name)
    return meta


def cleanup_cache(cache_dir: Path | None = None,
                  keep_days: int | None = None) -> list[str]:
    """按交易日滚动保留 N 版（默认 3），删除更旧版本（parquet+meta）。"""
    cache_dir = cache_dir or _default_dir()
    keep_days = keep_days if keep_days is not None else _default_keep_days()
    deleted: list[str] = []
    for d in list_cache_versions(cache_dir)[max(keep_days, 0):]:
        parquet_path, meta_path = _paths(cache_dir, d)
        for p in (parquet_path, meta_path):
            if p.exists():
                p.unlink()
                deleted.append(p.name)
    if deleted:
        logger.info("指标缓存清理：%s（保留 %d 版）", deleted, keep_days)
    return deleted


def load_cache(trade_date: date | None = None,
               cache_dir: Path | None = None) -> dict | None:
    """读缓存（不联网）：指定交易日或最新版本；返回 {meta, rows}，NaN 归一为 None。"""
    import pandas as pd

    cache_dir = cache_dir or _default_dir()
    if trade_date is None:
        versions = list_cache_versions(cache_dir)
        if not versions:
            return None
        trade_date = versions[0]
    parquet_path, meta_path = _paths(cache_dir, trade_date)
    if not parquet_path.exists():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    frame = pd.read_parquet(parquet_path)
    rows = []
    for rec in frame.to_dict("records"):
        rows.append({k: (None if pd.isna(v) else v) for k, v in rec.items()})
    return {"meta": meta, "rows": rows}
