# -*- coding: utf-8 -*-
"""财务数据离线批采 v4：10 年滚动窗口 + 剪枝 + 断点续跑 + 并发 + 指标生成。

v4 相对 v3 的变化：
- 只入库最近 --max-periods 期（默认 40 期 ≈ 10 年）；接口无时间参数，全量拉取后截断
- 采集成功后按票剪枝：删除超出窗口的更早报表/指标行（--prune/--dry-run/--archive-dir）
- 新增 --prune-only：只对已有数据剪枝，不联网

进度文件职责拆分（避免上一版 done 类型冲突）：
- /app/data/finance_done.txt     每成功一只追加一行代码（崩溃安全，续跑依据）
- /app/data/finance_failed.json  失败代码 → 原因（可读，便于重试）
- /app/data/finance_progress.json 纯统计（页面/接口读）
- /app/frontend/dist/assets/finance_progress.json 同上副本给静态页面轮询
"""
import argparse
import json
import os
import queue
import random
import threading
import time
from datetime import datetime

from app.models.base import SessionLocal
from app.models.market import StockFinance, StockFinIndicator, StockInfo
from app.services.finance import prune_finance, sync_finance_limited
from app.services.fin_indicators import sync_fin_indicators

DATA = "/app/data"
DONE_FILE = f"{DATA}/finance_done.txt"
FAIL_FILE = f"{DATA}/finance_failed.json"
PROGRESS = f"{DATA}/finance_progress.json"
PUBLIC = "/app/frontend/dist/assets/finance_progress.json"
PIDFILE = f"{DATA}/finance_backfill.pid"

_lock = threading.Lock()
_done_writes: list[str] = []


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def atomic_write(path: str, payload: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        print("进度写入失败:", path, e, flush=True)


def load_done() -> set[str]:
    if not os.path.exists(DONE_FILE):
        return set()
    try:
        with open(DONE_FILE, encoding="utf-8") as fh:
            return {line.strip() for line in fh if line.strip()}
    except Exception:
        return set()


def load_failed() -> dict:
    if os.path.exists(FAIL_FILE):
        try:
            with open(FAIL_FILE, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}
    return {}


def append_done(code: str) -> None:
    with open(DONE_FILE, "a", encoding="utf-8") as fh:
        fh.write(code + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--sleep", type=float, default=0.3)
    ap.add_argument("--retries", type=int, default=1)
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--max-periods", type=int, default=40,
                    help="保留的报告期数（40≈10年；0 表示不限制）")
    ap.add_argument("--dry-run", action="store_true", help="剪枝只报数不删除")
    ap.add_argument("--prune-only", action="store_true", help="只剪枝已有数据，不采集")
    ap.add_argument("--archive-dir", type=str, default="",
                    help="删除前归档 JSON 目录（默认空=不归档；全量跑建议留空，否则每只票 0.3~0.6MB 会吃掉省下的空间）")
    args = ap.parse_args()

    if args.reset:
        for p in (DONE_FILE, FAIL_FILE, PROGRESS):
            try:
                os.remove(p)
            except Exception:
                pass

    # ---------- 只剪枝模式 ----------
    if args.prune_only:
        db = SessionLocal()
        try:
            codes = [c for (c,) in db.query(StockFinance.code).distinct().order_by(StockFinance.code).all()]
            tot_f = tot_i = 0
            for code in codes:
                info = prune_finance(db, code, args.max_periods, dry_run=args.dry_run,
                                     archive_dir=args.archive_dir or None)
                tot_f += info.get("finance_deleted") or 0
                tot_i += info.get("indicator_deleted") or 0
                if (info.get("finance_deleted") or 0) > 0:
                    print(f"  {code}: 报表 -{info['finance_deleted']} 指标 -{info.get('indicator_deleted')}"
                          f" 保留 {info['kept']} 期 区间 {info.get('drop_range')}", flush=True)
            print(f"[剪枝完成] 票数={len(codes)} 报表 -{tot_f} 指标 -{tot_i} dry_run={args.dry_run}", flush=True)
        finally:
            db.close()
        return

    with open(PIDFILE, "w") as fh:
        fh.write(str(os.getpid()))

    db0 = SessionLocal()
    codes = [c for (c,) in db0.query(StockInfo.code).order_by(StockInfo.code).all()]
    db0.close()
    total = len(codes)

    done = load_done()
    failed = load_failed()
    started = now()

    pending = [c for c in codes if c not in done]
    batch = pending[: args.limit] if args.limit > 0 else pending
    print(f"待处理 {len(batch)} 只（已完成 {len(done)}/{total}，失败 {len(failed)}，并发 {args.workers}，"
          f"窗口 {args.max_periods} 期）", flush=True)

    q: "queue.Queue[str]" = queue.Queue()
    for c in batch:
        q.put(c)

    state = {"processed": 0, "t0": time.time(), "pruned_f": 0, "pruned_i": 0}

    def emit(last_code: str, status: str) -> None:
        with _lock:
            elapsed = max(time.time() - state["t0"], 1)
            rate = state["processed"] / elapsed * 60
            payload = {
                "total": total, "done": len(done), "failed": len(failed),
                "percent": round(len(done) / total * 100, 1) if total else 0,
                "batch_size": len(batch), "batch_processed": state["processed"],
                "last_code": last_code, "last_status": status,
                "updated_at": now(), "started_at": started,
                "rate_per_min": round(rate, 1),
                "eta_min": round(q.qsize() / rate, 1) if rate > 0 else None,
                "finished": len(done) + len(failed) >= total,
                "workers": args.workers,
                "max_periods": args.max_periods,
                "dry_run": args.dry_run,
                "pruned_finance": state["pruned_f"],
                "pruned_indicator": state["pruned_i"],
                "last_pruned": state.get("last_pruned", 0),
            }
            atomic_write(PROGRESS, payload)
            atomic_write(PUBLIC, payload)

    emit("", "running")

    def worker() -> None:
        db = SessionLocal()
        try:
            while True:
                try:
                    code = q.get_nowait()
                except queue.Empty:
                    break
                ok, err = False, ""
                pruned = 0
                for attempt in range(args.retries + 1):
                    try:
                        stats = sync_finance_limited(db, code, args.max_periods)
                        if int(stats.get("received") or 0) <= 0:
                            raise RuntimeError("返回 0 条记录（限流或字段变动）")
                        # 先剪枝再算指标：指标只为窗口内期数生成
                        pr = prune_finance(db, code, args.max_periods, dry_run=args.dry_run,
                                           archive_dir=args.archive_dir or None)
                        pruned = pr.get("finance_deleted") or 0
                        with _lock:
                            state["pruned_f"] += pruned
                            state["pruned_i"] += pr.get("indicator_deleted") or 0
                        sync_fin_indicators(db, code)   # 关键：生成指标，模型才读得到
                        ok = True
                        break
                    except Exception as e:  # noqa: BLE001
                        err = str(e)[:200]
                        # 北交所（4/8 开头）在東财 F10 三表接口无数据：标 unsupported，不算失败
                        if code.startswith(("4", "8", "92")) and "三表均未取到数据" in err:
                            with _lock:
                                state["unsupported"] = state.get("unsupported", 0) + 1
                            ok = True
                            err = ""
                            break
                        if attempt < args.retries:
                            time.sleep(2)
                with _lock:
                    state["processed"] += 1
                    state["last_pruned"] = pruned
                    if ok:
                        done.add(code)
                        failed.pop(code, None)
                        append_done(code)
                    else:
                        failed[code] = err
                        atomic_write(FAIL_FILE, failed)
                emit(code, "ok" if ok else "failed")
                if state["processed"] % 50 == 0:
                    db.expire_all()
                time.sleep(random.random() * args.sleep)
        finally:
            db.close()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, args.workers))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    emit("", "finished")
    db = SessionLocal()
    try:
        print("已入库 报表行=%d 指标行=%d 本次剪枝 报表-%d 指标-%d"
              % (db.query(StockFinance).count(), db.query(StockFinIndicator).count(),
                 state["pruned_f"], state["pruned_i"]), flush=True)
    finally:
        db.close()


if __name__ == "__main__":
    main()