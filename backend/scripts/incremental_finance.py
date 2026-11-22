# -*- coding: utf-8 -*-
"""财务增量采集：公告驱动（谁披露就采谁）。

原理：东财业绩报表接口支持按公告日期过滤（实测 2026Q3 全市场 5813 家 → 加
NOTICE_DATE>=2026-11-18 后只剩 7 家），所以每次只需拉「最近 N 天公告」的清单，
再对这批代码跑单票采集（窗口 40 期）+ 剪枝 + 指标 + 公告日期回填。

与全量回填的分工：
- backfill_finance.py：一次性补历史（5580 只，15~30 小时，窗口 40 期）
- incremental_finance.py：日常增量（每天几十~几百只，几分钟，幂等）

用法：
  python incremental_finance.py --days 3 --workers 3
  python incremental_finance.py --dry-run          # 只列清单，不入库
  python incremental_finance.py --codes 600519,000001   # 定向补采
  python incremental_finance.py --since 2026-10-01 # 指定起点补采

重复采同一只票是幂等的（upsert），所以默认每次覆盖最近 3 天，宁可重采不漏采。
北交所（4/8 开头）票在东财 F10 三表接口返回空，标记 unsupported 而非失败。
"""
import argparse
import json
import os
import queue
import random
import threading
import time
from datetime import date, datetime, timedelta

import requests

DATA = "/app/data"
STATE = f"{DATA}/finance_incremental.json"
PUBLIC = "/app/frontend/dist/assets/finance_incremental.json"
LOG = f"{DATA}/finance_incremental.log"
PIDFILE = f"{DATA}/finance_incremental.pid"
ARCHIVE = ""  # 默认不归档；需要时用 --archive-dir 指定

YJBB_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
YJYG_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Referer": "https://data.eastmoney.com/"}
YJYG_HEAD = ('(SECURITY_TYPE_CODE in ("058001001","058001008"))'
             '(TRADE_MARKET_CODE!="069001017")')

_lock = threading.Lock()


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def atomic_write(path: str, payload: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        log(f"状态写入失败 {path}: {e}")


def is_bj(code: str) -> bool:
    """北交所/新三板：东财 F10 三表接口不收录，采集必然为空。"""
    c = str(code or "").strip()
    return c.startswith(("4", "8", "92"))


def last_quarter_ends(n: int = 6, today: date | None = None) -> list:
    today = today or date.today()
    out = []
    for y in range(today.year, today.year - 4, -1):
        for md in ((12, 31), (9, 30), (6, 30), (3, 31)):
            d = date(y, *md)
            if d <= today:
                out.append(d)
    out.sort(reverse=True)
    return out[:n]


def fetch_json(url: str, params: dict, retries: int = 2, timeout: int = 25) -> dict:
    last = None
    for i in range(retries + 1):
        try:
            return requests.get(url, params=params, headers=UA, timeout=timeout).json()
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries:
                time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"请求失败 {url}: {last}")


def fetch_all(url: str, params: dict, max_pages: int = 30) -> list:
    rows = []
    page = 1
    while page <= max_pages:
        p = dict(params)
        p["pageNumber"] = str(page)
        d = fetch_json(url, p)
        res = d.get("result") or {}
        data = res.get("data") or []
        rows.extend(data)
        pages = int(res.get("pages") or 0)
        if page >= pages or not data:
            break
        page += 1
    return rows


def _day(v) -> str:
    return str(v)[:10] if v else ""


def fetch_announce_pairs(code: str) -> list:
    """按代码拉全部报告期的公告日期（东财业绩报表，实测 600519 返回 104 期）。

    这是 announce_date 的可靠来源：东财三表接口本身不下发公告日，
    而业绩报表接口按 SECURITY_CODE 过滤可拿到 REPORTDATE → NOTICE_DATE 全量映射。
    """
    params = {"reportName": "RPT_LICO_FN_CPD", "columns": "ALL", "pageSize": "500",
              "pageNumber": "1", "sortColumns": "REPORTDATE", "sortTypes": "-1",
              "source": "WEB", "client": "WEB",
              "filter": f'(SECURITY_CODE="{code}")'}
    try:
        rows = fetch_json(YJBB_URL, params).get("result", {}).get("data") or []
    except Exception as e:  # noqa: BLE001
        log(f"公告日期查询失败 {code}: {e}")
        return []
    return [(_day(r.get("REPORTDATE")), _day(r.get("NOTICE_DATE")))
            for r in rows if r.get("REPORTDATE") and r.get("NOTICE_DATE")]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3, help="回看天数（默认 3 天，幂等可重采）")
    ap.add_argument("--since", type=str, default="", help="指定公告日期起点 YYYY-MM-DD")
    ap.add_argument("--periods", type=int, default=6, help="检查最近几个报告期")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--max-periods", type=int, default=40, help="保留报告期数（40≈10年）")
    ap.add_argument("--codes", type=str, default="", help="定向采集：逗号分隔代码，跳过清单拉取")
    ap.add_argument("--archive-dir", type=str, default="", help="剪枝前归档目录（默认不归档）")
    ap.add_argument("--dry-run", action="store_true", help="只列清单，不入库")
    args = ap.parse_args()

    since = args.since or (date.today() - timedelta(days=max(args.days, 1))).isoformat()
    periods = [d.isoformat() for d in last_quarter_ends(args.periods)]
    started = now()
    log(f"增量启动 since={since} quarters={periods} dry_run={args.dry_run} workers={args.workers}")

    with open(PIDFILE, "w") as fh:
        fh.write(str(os.getpid()))

    base = {"columns": "ALL", "pageSize": "500", "source": "WEB", "client": "WEB"}

    discovered = {}
    forecast = {}
    errors = []
    if not args.codes:
        for rp in periods:
            params = dict(base)
            params.update({
                "sortColumns": "UPDATE_DATE,SECURITY_CODE", "sortTypes": "-1,-1",
                "reportName": "RPT_LICO_FN_CPD",
                "filter": f"(REPORTDATE='{rp}')(NOTICE_DATE>='{since}')",
            })
            try:
                rows = fetch_all(YJBB_URL, params)
                for r in rows:
                    code = str(r.get("SECURITY_CODE") or "").strip()
                    if not code:
                        continue
                    slot = discovered.setdefault(code, {"code": code,
                                                        "name": r.get("SECURITY_NAME_ABBR") or "",
                                                        "periods": {}})
                    slot["periods"][rp] = _day(r.get("NOTICE_DATE"))
                log(f"报表 {rp}: 新披露 {len(rows)} 条")
            except Exception as e:  # noqa: BLE001
                errors.append(f"报表 {rp}: {e}")
                log(f"报表 {rp} 失败: {e}")

            params = dict(base)
            params.update({
                "sortColumns": "NOTICE_DATE,SECURITY_CODE", "sortTypes": "-1,-1",
                "reportName": "RPT_PUBLIC_OP_NEWPREDICT",
                "filter": YJYG_HEAD + f"(REPORT_DATE='{rp}')(NOTICE_DATE>='{since}')",
            })
            try:
                rows = fetch_all(YJYG_URL, params)
                for r in rows:
                    code = str(r.get("SECURITY_CODE") or "").strip()
                    if code:
                        forecast[code] = {"code": code, "name": r.get("SECURITY_NAME_ABBR") or "",
                                          "period": rp, "notice": _day(r.get("NOTICE_DATE")),
                                          "type": r.get("PREDICT_TYPE") or ""}
                log(f"预告 {rp}: 新披露 {len(rows)} 条")
            except Exception as e:  # noqa: BLE001
                errors.append(f"预告 {rp}: {e}")
                log(f"预告 {rp} 失败: {e}")
    else:
        for c in (x.strip() for x in args.codes.split(",")):
            if c:
                discovered[c] = {"code": c, "name": "", "periods": {}}
        log(f"定向采集 {len(discovered)} 只：{list(discovered)}")

    codes = sorted(discovered)
    bj = [c for c in codes if is_bj(c)]
    log(f"需采集 {len(codes)} 只（其中北交所 {len(bj)} 只，三表接口无数据将标 unsupported）；预告提示 {len(forecast)} 只")

    payload = {
        "running": True, "dry_run": bool(args.dry_run), "since": since,
        "quarters": periods, "started_at": started, "updated_at": now(),
        "discovered": len(codes), "forecast": len(forecast),
        "collected": 0, "failed": 0, "unsupported": 0, "finished": False,
        "codes": [{"code": c, "name": discovered[c]["name"],
                   "periods": sorted(discovered[c]["periods"]),
                   "bj": is_bj(c)} for c in codes[:200]],
        "forecast_list": [forecast[c] for c in sorted(forecast)[:100]],
        "errors": errors[:10],
    }
    atomic_write(STATE, payload)
    atomic_write(PUBLIC, payload)

    if args.dry_run:
        for c in codes:
            log(f"  [dry] {c} {discovered[c]['name']} 期 {list(discovered[c]['periods'])}"
                f"{' (北交所/三表无源)' if is_bj(c) else ''}")
        payload.update({"running": False, "finished": True, "ended_at": now()})
        atomic_write(STATE, payload)
        atomic_write(PUBLIC, payload)
        print(f"[dry-run] 发现 {len(codes)} 只需采集，未入库", flush=True)
        return

    if not codes:
        payload.update({"running": False, "finished": True, "ended_at": now()})
        atomic_write(STATE, payload)
        atomic_write(PUBLIC, payload)
        print("无新披露，结束", flush=True)
        return

    from app.models.base import SessionLocal
    from app.services.finance import prune_finance, set_announce_dates, sync_finance_limited
    from app.services.fin_indicators import sync_fin_indicators

    q: "queue.Queue[str]" = queue.Queue()
    for c in codes:
        q.put(c)
    state = {"ok": 0, "failed": 0, "unsupported": 0, "pruned": 0, "t0": time.time()}

    def emit(last_code: str, status: str) -> None:
        with _lock:
            payload.update({
                "running": True, "updated_at": now(),
                "collected": state["ok"], "failed": state["failed"],
                "unsupported": state["unsupported"], "pruned": state["pruned"],
                "last_code": last_code, "last_status": status,
            })
            atomic_write(STATE, payload)
            atomic_write(PUBLIC, payload)

    def worker() -> None:
        db = SessionLocal()
        try:
            while True:
                try:
                    code = q.get_nowait()
                except queue.Empty:
                    break
                err = ""
                ok = False
                try:
                    if is_bj(code):
                        # 北交所：东财 F10 三表不收录，直接跳（未来若收录再放开）
                        raise _Unsupported("北交所：东财三表接口无数据")
                    stats = sync_finance_limited(db, code, args.max_periods)
                    if int(stats.get("received") or 0) <= 0:
                        raise RuntimeError("返回 0 条记录")
                    pr = prune_finance(db, code, args.max_periods, archive_dir=(args.archive_dir or None))
                    # 公告日期：优先按代码拉全量期映射（覆盖库存内所有期，不止最近披露的期）
                    pairs = fetch_announce_pairs(code) or list(discovered[code]["periods"].items())
                    if pairs:
                        set_announce_dates(db, code, pairs)
                    sync_fin_indicators(db, code)
                    with _lock:
                        state["ok"] += 1
                        state["pruned"] += pr.get("finance_deleted") or 0
                    ok = True
                except _Unsupported as e:
                    with _lock:
                        state["unsupported"] += 1
                    log(f"跳过 {code}: {e}")
                except Exception as e:  # noqa: BLE001
                    err = str(e)[:200]
                    with _lock:
                        state["failed"] += 1
                        payload["fail_detail"] = payload.get("fail_detail") or {}
                        payload["fail_detail"][code] = err
                    log(f"采集失败 {code}: {err}")
                emit(code, "ok" if ok else ("unsupported" if not err else "failed"))
                time.sleep(random.random() * 0.3)
        finally:
            db.close()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, args.workers))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    payload.update({
        "running": False, "finished": True, "ended_at": now(), "updated_at": now(),
        "collected": state["ok"], "failed": state["failed"],
        "unsupported": state["unsupported"], "pruned": state["pruned"],
        "elapsed_sec": round(time.time() - state["t0"], 1),
    })
    atomic_write(STATE, payload)
    atomic_write(PUBLIC, payload)
    log(f"增量完成：发现 {len(codes)} 成功 {state['ok']} 跳过 {state['unsupported']} "
        f"失败 {state['failed']} 剪枝 {state['pruned']}")
    print(f"[完成] 发现 {len(codes)} 成功 {state['ok']} 跳过 {state['unsupported']} "
          f"失败 {state['failed']}", flush=True)


class _Unsupported(Exception):
    """数据源不覆盖（不计失败，避免污染重试队列）。"""


if __name__ == "__main__":
    main()