# -*- coding: utf-8 -*-
"""补齐某个交易日的日线（按缺票清单定向补采）。

场景：某天采集时段数据源还没出全（例如 15:30 首次采集时部分票当日 K 线缺失），
库里那天的行数只有前一日的一半，估值扫描就用「半个市场」出结果。此时按
「当天缺失的代码清单」逐票补采，走既有 plan_ranges → collect_with_fallback →
upsert 链路，幂等且可重跑。

用法：
  python backfill_kline.py                        # 补「库内最新交易日」
  python backfill_kline.py --date 2026-11-20      # 指定交易日
  python backfill_kline.py --workers 3 --dry-run  # 只列缺票数，不采

后台跑（容器内，脱离 SSH 会话）：
  docker exec -w /app -e PYTHONPATH=/app dsa-app \\
    sh -c "setsid nohup python -u /app/data/backfill_kline.py > /app/data/bf.out 2>&1 < /dev/null &"

注意：
- 不要同时起两个实例：upsert_kline 是「查后写」，并发会撞主键（Duplicate entry）
- 缺票查询用 NOT IN + (date) 索引；LEFT JOIN 版本在这张 1400 万行表上要 20s+
- 退市/停牌票当日无 K 线属正常，计 empty 而非失败
"""
import argparse
import queue
import threading
import time
from datetime import date

from sqlalchemy import text

from app.models.base import SessionLocal
from app.services.kline import sync_kline_for_code

LOG = "/app/data/kline_backfill_%s.log"


def log(msg: str, path: str) -> None:
    line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def latest_trade_date() -> date:
    db = SessionLocal()
    try:
        d = db.execute(text("SELECT MAX(date) FROM stock_kline")).scalar()
        return d or date.today()
    finally:
        db.close()


def missing_codes(target: date) -> list[str]:
    """当天没有 K 线的代码清单（用 (date) 索引，毫秒级）。"""
    db = SessionLocal()
    try:
        rows = db.execute(text(
            "SELECT code FROM stock_info WHERE code NOT IN "
            "(SELECT code FROM stock_kline WHERE date = :d) ORDER BY code"
        ), {"d": target}).fetchall()
        return [r[0] for r in rows]
    finally:
        db.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="交易日 YYYY-MM-DD，缺省=库内最新")
    ap.add_argument("--workers", type=int, default=3, help="并发线程数（默认 3）")
    ap.add_argument("--dry-run", action="store_true", help="只列缺票数，不采集")
    args = ap.parse_args()

    target = date.fromisoformat(args.date) if args.date else latest_trade_date()
    log_path = LOG % target.isoformat()
    codes = missing_codes(target)
    log("start target=%s missing=%d workers=%d" % (target, len(codes), args.workers), log_path)
    if args.dry_run or not codes:
        return

    q: queue.Queue = queue.Queue()
    for c in codes:
        q.put(c)
    done = {"ok": 0, "err": 0, "inserted": 0, "empty": 0}
    lock = threading.Lock()

    def worker() -> None:
        while True:
            try:
                code = q.get_nowait()
            except queue.Empty:
                return
            db = SessionLocal()
            try:
                r = sync_kline_for_code(db, code, end_date=target)
                n = int(r.get("inserted") or 0)
                with lock:
                    done["ok"] += 1
                    done["inserted"] += n
                    if n == 0:
                        done["empty"] += 1
            except Exception as exc:  # 单票失败不影响其余
                db.rollback()
                with lock:
                    done["err"] += 1
                if done["err"] % 20 == 1:
                    log("ERR %s %s" % (code, str(exc)[:120]), log_path)
            finally:
                db.close()
            with lock:
                n_done = done["ok"] + done["err"]
            if n_done % 200 == 0:
                log("progress done=%d ok=%d err=%d inserted=%d empty=%d" % (
                    n_done, done["ok"], done["err"], done["inserted"], done["empty"]), log_path)
            time.sleep(0.1)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, args.workers))]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log("DONE ok=%d err=%d inserted=%d empty=%d elapsed=%.0fs" % (
        done["ok"], done["err"], done["inserted"], done["empty"], time.time() - t0), log_path)


if __name__ == "__main__":
    main()