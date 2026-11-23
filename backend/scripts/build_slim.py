# -*- coding: utf-8 -*-
"""财报额外字段物化：把 stock_finance.extra 里的总股本抽到小表。

背景：stock_finance 单表 3.1GB（19 万行 × 约 200 个 JSON 键），带 extra 全表读出
约 160~190s，估值快照每次请求都读它会把接口拖到分钟级。这里把估值真正要用的
`SHARE_CAPITAL`（总股本）物化到 19 万行、约 10MB 的小表 `stock_fin_extra_slim`，
估值侧按 (code, report_date) 索引读，降到毫秒级。

分工：
- 本脚本：财报入库/回填后跑一次，刷新小表（幂等，ON DUPLICATE KEY UPDATE）
- services/valuation.py：`_extra_map` 读小表，表缺失时自动回退旧 JSON 读法

用法：
  python build_slim.py                # 全量刷新（按 code 键集分页，边读边写）
  python build_slim.py --batch 8000   # 调大批次（默认 4000）
  python build_slim.py --log /app/data/build_slim.log

耗时：冷缓存约 20~50 分钟（瓶颈在 3GB 大表的随机读），缓冲池热时 3~5 分钟。
"""
import argparse
import json
import os
import time
from datetime import date

from sqlalchemy import text

from app.models.base import SessionLocal

LOG = "/app/data/build_slim.log"

DDL = """
CREATE TABLE IF NOT EXISTS stock_fin_extra_slim (
  code VARCHAR(12) NOT NULL,
  report_date DATE NOT NULL,
  share_capital DOUBLE NULL,
  PRIMARY KEY (code, report_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def log(msg: str, path: str = LOG) -> None:
    line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass  # 日志写不进去不影响主流程


def share_of(extra) -> float | None:
    """从 extra 里取总股本；取不到返回 None（不猜、不补 0）。"""
    data = extra
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return None
    if not isinstance(data, dict):
        return None
    try:
        val = float(data.get("SHARE_CAPITAL"))
    except (TypeError, ValueError):
        return None
    return val if val == val else None  # 过滤 NaN


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=4000, help="每批读取行数（默认 4000）")
    ap.add_argument("--log", default=LOG, help="日志路径")
    args = ap.parse_args()

    db = SessionLocal()
    db.execute(text(DDL))
    db.commit()

    start = time.time()
    cursor = ("", date(1900, 1, 1))
    buf: list[dict] = []
    total = 0
    insert_sql = text(
        "INSERT INTO stock_fin_extra_slim (code, report_date, share_capital) "
        "VALUES (:c, :d, :s) ON DUPLICATE KEY UPDATE share_capital = VALUES(share_capital)"
    )
    while True:
        rows = db.execute(text(
            "SELECT code, report_date, extra FROM stock_finance "
            "WHERE (code, report_date) > (:c, :d) ORDER BY code, report_date LIMIT :n"
        ), {"c": cursor[0], "d": cursor[1], "n": args.batch}).fetchall()
        if not rows:
            break
        for code, rd, extra in rows:
            buf.append({"c": code, "d": rd, "s": share_of(extra)})
            cursor = (code, rd)
        if len(buf) >= args.batch:
            db.execute(insert_sql, buf)
            db.commit()
            total += len(buf)
            buf = []
            if total % (args.batch * 10) == 0:
                log("progress %d rows, %.0fs" % (total, time.time() - start), args.log)
    if buf:
        db.execute(insert_sql, buf)
        db.commit()
        total += len(buf)

    stat = db.execute(text(
        "SELECT COUNT(*), COUNT(share_capital) FROM stock_fin_extra_slim"
    )).fetchone()
    log("DONE rows=%d with_share=%d inserted_now=%d elapsed=%.0fs" % (
        stat[0], stat[1], total, time.time() - start), args.log)
    db.close()


if __name__ == "__main__":
    main()