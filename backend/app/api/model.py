"""估值模型接口（边风炜估值选股 · /api/model）。

三个端点：
- GET  /api/model/rules        规则元数据（权重、阈值、风格清单）
- POST /api/model/scan         全市场扫描（可过滤风格/档位，默认返回前 200）
- GET  /api/model/evaluate/{code}  单票评估（含逐条命中与区间位置）

口径：与 services.bw_model 一致；不修改任何既有接口行为。
"""
from __future__ import annotations

from datetime import date

import os
import subprocess
import sys

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.base import get_db
from app.services import bw_model

router = APIRouter(prefix="/api/model", tags=["model"])


class ScanRequest(BaseModel):
    trade_date: str | None = Field(default=None, description="交易日 YYYY-MM-DD，缺省=库内最新")
    style: str | None = Field(default=None, description="风格过滤：bank/utility/consumer/cyclical/growth/insurance/property")
    verdict: str | None = Field(default=None, description="档位过滤：pass/watch/reject")
    limit: int = Field(default=200, ge=1, le=2000)


@router.get("/rules")
def model_rules():
    """规则元数据（前端规则配置页渲染用；不含任何账号/密钥信息）。"""
    return bw_model.rules_meta()


@router.post("/scan")
def model_scan(payload: ScanRequest, db: Session = Depends(get_db)):
    """全市场扫描。同步返回；数据缺失的门槛在 checks 中标注 skipped。"""
    trade_date: date | None = None
    if payload.trade_date:
        try:
            trade_date = date.fromisoformat(payload.trade_date[:10])
        except ValueError:
            raise AppError(400, "trade_date 需为 YYYY-MM-DD")
    if payload.style and payload.style not in bw_model.RULES:
        raise AppError(400, f"未知风格: {payload.style}")
    if payload.verdict and payload.verdict not in ("pass", "watch", "reject"):
        raise AppError(400, "verdict 仅支持 pass/watch/reject")
    return bw_model.scan(db, trade_date=trade_date, style=payload.style,
                         verdict=payload.verdict, limit=payload.limit)


@router.get("/evaluate/{code}")
def model_evaluate(code: str, trade_date: str | None = None, db: Session = Depends(get_db)):
    """单票评估：档位、分数、逐条规则命中、区间位置、财报红旗。"""
    d: date | None = None
    if trade_date:
        try:
            d = date.fromisoformat(trade_date[:10])
        except ValueError:
            raise AppError(400, "trade_date 需为 YYYY-MM-DD")
    out = bw_model.evaluate(db, code, trade_date=d)
    if out.get("error"):
        raise AppError(404, out["error"])
    return out

# ---------- 财务回填（可续跑脚本的后台入口） ----------
PROGRESS_FILE = "/app/data/finance_progress.json"
LOG_FILE = "/app/data/finance_backfill.log"
PID_FILE = "/app/data/finance_backfill.pid"


def _running() -> bool:
    """只看 pid 不够：进程崩溃后 pid 可能被复用，需校验命令行确为回填脚本。"""
    try:
        with open(PID_FILE) as fh:
            pid = int(fh.read().strip())
        os.kill(pid, 0)
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmd = fh.read().decode("utf-8", "ignore")
        return "backfill_finance.py" in cmd
    except Exception:
        return False


@router.post("/collect/start")
def model_collect_start(limit: int = 0, workers: int = 4, reset: bool = False):
    """后台启动财务回填（断点续跑）。返回当前是否已在跑。"""
    if _running():
        return {"ok": False, "running": True, "message": "已有回填进程在运行，可调 /api/model/collect/status 查看进度"}
    cmd = [sys.executable, "-u", "/app/data/backfill_finance.py",
           "--workers", str(max(1, min(workers, 12)))]
    if limit > 0:
        cmd += ["--limit", str(limit)]
    if reset:
        cmd += ["--reset"]
    with open(LOG_FILE, "ab") as log:
        log.write(("\n==== 启动 %s ====\n" % " ".join(cmd)).encode())
        log.flush()
        env = {**os.environ, "PYTHONPATH": "/app"}
        subprocess.Popen(cmd, cwd="/app", env=env, stdout=log, stderr=log, start_new_session=True)
    return {"ok": True, "running": True, "message": "已启动后台回填", "cmd": " ".join(cmd)}


@router.get("/collect/status")
def model_collect_status():
    """读取回填进度（脚本写的 JSON），并返回进程是否在跑。"""
    import json as _json
    progress = None
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, encoding="utf-8") as fh:
                progress = _json.load(fh)
        except Exception:
            progress = None
    return {"running": _running(), "progress": progress}
# ---------------- 增量采集（公告驱动，2026-11-22 追加） ----------------
INCR_FILE = "/app/data/finance_incremental.json"
INCR_PID = "/app/data/finance_incremental.pid"
INCR_LOG = "/app/data/finance_incremental.log"


def _incr_running() -> bool:
    """增量进程存活判断：pid 存活 + 命令行确认是增量脚本（防 pid 复用误判）。"""
    try:
        with open(INCR_PID) as fh:
            pid = int(fh.read().strip())
        os.kill(pid, 0)
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmd = fh.read().decode("utf-8", "ignore")
        return "incremental_finance.py" in cmd
    except Exception:
        return False


@router.post("/collect/incremental")
def model_collect_incremental(days: int = 3, workers: int = 3, dry_run: bool = False):
    """后台启动增量采集：只采最近 N 天有新公告的票（预告/季报驱动）。"""
    if _incr_running():
        return {"ok": False, "running": True,
                "message": "增量进程正在运行，可调 /api/model/collect/incremental/status 查看"}
    if _running():
        return {"ok": False, "running": True, "message": "全量回填正在运行，请等它结束再跑增量"}
    cmd = [sys.executable, "-u", "/app/data/incremental_finance.py",
           "--days", str(max(1, min(days, 30))),
           "--workers", str(max(1, min(workers, 8)))]
    if dry_run:
        cmd += ["--dry-run"]
    with open(INCR_LOG, "ab") as log:
        log.write(("\n==== 启动 %s ====\n" % " ".join(cmd)).encode())
        log.flush()
        env = {**os.environ, "PYTHONPATH": "/app"}
        subprocess.Popen(cmd, cwd="/app", env=env, stdout=log, stderr=log, start_new_session=True)
    return {"ok": True, "running": True, "message": "已启动增量采集", "cmd": " ".join(cmd)}


@router.get("/collect/incremental/status")
def model_incremental_status():
    """读取增量状态（脚本写的 JSON）+ 进程是否在跑。"""
    import json as _json
    state = None
    if os.path.exists(INCR_FILE):
        try:
            with open(INCR_FILE, encoding="utf-8") as fh:
                state = _json.load(fh)
        except Exception:
            state = None
    return {"running": _incr_running(), "state": state}