"""T1.11 实机切源演练脚本（部署后执行，产出 M2 验收材料）。

在业务库上真实执行「停用 K 线主源 → 自动降级备源 → 恢复」演练，并输出
Markdown 演练记录（接口配置快照 / 切换记录 / 数据完整性核对 / 限流参数 /
全量采集计时基线），可直接粘贴进 docs/开发进度文档.md T1.11 备注。

演练走生产代码路径（source_registry 选路 → collect_chain 降级 → upsert 入库），
采集窗口默认最近 30 天（约 20 根日线），对数据源压力极小。

用法（容器内或能连业务库的主机，DATABASE_URL 指向业务库）：
    python scripts/drill_switch_source.py                  # 单标的切源演练
    python scripts/drill_switch_source.py --code 600000
    python scripts/drill_switch_source.py --full           # 附带全量采集计时基线（目标 ≤5 分钟）
    python scripts/drill_switch_source.py --out drill.md   # 记录写入文件

注意事项：
- 演练会临时停用 K 线主源，结束（含异常）后自动恢复原配置；
- 需要真实数据源可达（mootdx / akshare 等）；仅读写业务库与接口配置表；
- 服务函数命名避开 test_ 前缀（pytest 误收集教训，见 T1.3 备注）。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/ 入口

from sqlalchemy import func  # noqa: E402

from app.models.base import SessionLocal  # noqa: E402
from app.models.market import StockInfo, StockKline  # noqa: E402
from app.models.system import DataSourceConfig, DataSourceLog  # noqa: E402
from app.services.collect_chain import collect_with_fallback  # noqa: E402
from app.services.kline import upsert_kline  # noqa: E402
from app.services.source_registry import pick_sources, update_source  # noqa: E402

DRILL_WINDOW_DAYS = 30  # 演练采集窗口（对数据源压力极小）


# ---------- 工具 ----------

def _mask_db_url(url: str) -> str:
    """脱敏数据库连接串（演练记录不带口令）。"""
    if "@" in url and "//" in url:
        head, _, tail = url.partition("//")
        creds, _, hostdb = tail.partition("@")
        user = creds.partition(":")[0]
        return f"{head}//{user}:***@{hostdb}"
    return url


def _range_for(end: date) -> tuple[date, date]:
    return end - timedelta(days=DRILL_WINDOW_DAYS), end


def _collect_window(db, code: str, end: date) -> tuple[list[dict], dict, float]:
    """走采集链取一个演练窗口并入库，返回 (原始行, 入库统计, 耗时秒)。"""
    start, end = _range_for(end)
    t0 = time.monotonic()
    records = collect_with_fallback(
        db, "kline", code=code,
        start_date=start.isoformat(), end_date=end.isoformat(),
    )
    stats = upsert_kline(db, records)
    return records, stats, time.monotonic() - t0


def _served_by(db, since_id: int) -> list[tuple[str, str, str]]:
    """演练窗口内的 data_source_log 记录（来源、状态、消息）。"""
    rows = (
        db.query(DataSourceLog)
        .filter(DataSourceLog.id > since_id, DataSourceLog.data_type == "kline")
        .order_by(DataSourceLog.id.asc())
        .all()
    )
    return [(r.source_name, r.status, r.message) for r in rows]


def _compare_rows(base_rows: list[dict], alt_rows: list[dict]) -> dict:
    """两源数据完整性核对：按日期对齐比对收盘价（不复权价应一致）。"""
    base = {str(r.get("date"))[:10]: float(r["close"]) for r in base_rows if r.get("close")}
    alt = {str(r.get("date"))[:10]: float(r["close"]) for r in alt_rows if r.get("close")}
    common = sorted(set(base) & set(alt))
    matched = [d for d in common if abs(base[d] - alt[d]) < 0.01]
    return {
        "base_days": len(base), "alt_days": len(alt),
        "common_days": len(common), "matched_days": len(matched),
        "mismatch": [d for d in common if d not in matched],
    }


# ---------- 演练 ----------

def run_drill(code: str, end: date, with_full: bool = False) -> str:
    db = SessionLocal()
    lines: list[str] = []
    try:
        # 0. 接口配置与限流参数快照（基线记录）
        all_sources = (
            db.query(DataSourceConfig)
            .order_by(DataSourceConfig.data_type.asc(), DataSourceConfig.priority.asc())
            .all()
        )
        kline_sources = [s for s in all_sources if s.data_type == "kline"]
        if not kline_sources:
            raise SystemExit("kline 数据类型无接口配置，无法演练（请先跑采集流水线补种）")
        primary = next((s for s in kline_sources if s.enabled), None)
        if primary is None:
            raise SystemExit("kline 无启用接口，无法演练")

        from app.core.config import get_settings
        lines += [
            "# T1.11 切源演练记录（实机）",
            "",
            f"- 演练时间：{datetime.now():%Y-%m-%d %H:%M:%S}（容器 TZ={time.tzname}）",
            f"- 数据库：`{_mask_db_url(get_settings().database_url)}`",
            f"- 演练标的：{code}（窗口 {(end - timedelta(days=DRILL_WINDOW_DAYS))} ~ {end}，"
            f"约 {DRILL_WINDOW_DAYS} 日历天）",
            f"- K线主源：{primary.name}（priority={primary.priority}，QPS={primary.qps_limit}）",
            "",
            "## 接口配置与限流参数快照",
            "",
            "| 数据类型 | 接口名称 | 角色 | 优先级 | QPS上限 | 超时(秒) | 启用 |",
            "|---|---|---|---|---|---|---|",
        ]
        for s in all_sources:
            lines.append(
                f"| {s.data_type} | {s.name} | {s.role} | {s.priority} | "
                f"{s.qps_limit} | {s.timeout_sec} | {'是' if s.enabled else '否'} |"
            )

        # 1. 基线：主源正常采集
        log_mark = db.query(func.max(DataSourceLog.id)).scalar() or 0
        base_rows, base_stats, base_secs = _collect_window(db, code, end)
        base_log = _served_by(db, log_mark)

        # 2. 切源演练：停用主源 → 自动降级备源（结束后恢复原配置，异常亦恢复）
        enabled_backup = primary.enabled
        update_source(db, primary.id, enabled=False)
        switched = [s.name for s in pick_sources(db, "kline")]
        log_mark = db.query(func.max(DataSourceLog.id)).scalar() or 0
        try:
            alt_rows, alt_stats, alt_secs = _collect_window(db, code, end)
            alt_log = _served_by(db, log_mark)
            switch_error = ""
        except Exception as e:  # 演练失败也要恢复配置并留记录
            alt_rows, alt_stats, alt_secs, alt_log, switch_error = [], {}, 0.0, [], str(e)
        finally:
            update_source(db, primary.id, enabled=enabled_backup)  # 恢复原配置

        # 3. 数据完整性核对
        cmp = _compare_rows(base_rows, alt_rows)
        ok_days = f"{cmp['matched_days']}/{cmp['common_days']}"

        lines += [
            "",
            "## 演练步骤与结果",
            "",
            "| 步骤 | 动作 | 结果 | 耗时 | data_source_log |",
            "|---|---|---|---|---|",
            f"| 1 基线 | 主源正常采集窗口 | {len(base_rows)} 行（入库 +{base_stats.get('inserted', 0)}"
            f"/更新 {base_stats.get('updated', 0)}） | {base_secs:.1f}s | {base_log} |",
            f"| 2 停用主源 | 设置页停用 `{primary.name}`，选路 → {switched} | "
            + (f"{len(alt_rows)} 行（入库 +{alt_stats.get('inserted', 0)}"
               f"/更新 {alt_stats.get('updated', 0)}）" if not switch_error else f"异常：{switch_error}")
            + f" | {alt_secs:.1f}s | {alt_log} |",
            f"| 3 恢复 | 恢复 `{primary.name}` 原启用状态 | 完成 | — | — |",
            "",
            "## 数据完整性核对（两源逐日收盘价比对）",
            "",
            f"- 主源 {cmp['base_days']} 个交易日 / 备源 {cmp['alt_days']} 个交易日；"
            f"共同 {cmp['common_days']} 日，收盘价一致 {ok_days}"
            + (f"，不一致日期：{cmp['mismatch']}" if cmp["mismatch"] else ""),
            f"- 结论：{'切换过程无报错、数据完整 ✅' if not switch_error and cmp['common_days'] > 0 and not cmp['mismatch'] else '存在差异，需人工核对 ⚠️'}",
        ]

        # 4. 可选：全量采集计时基线（T1.10 递延项）
        if with_full:
            from app.services.pipeline import run_pipeline
            t0 = time.monotonic()
            fstats = run_pipeline(db, "full", run_date=end)
            fsecs = time.monotonic() - t0
            lines += [
                "",
                "## 全量采集计时基线（T1.10 递延）",
                "",
                f"- `run_pipeline(full)` 耗时 **{fsecs:.0f}s**（目标 ≤300s）："
                f"{'达标 ✅' if fsecs <= 300 else '超标，需优化 ⚠️'}",
                f"- 分线统计：`{fstats}`",
            ]

        lines += [
            "",
            "> 由 backend/scripts/drill_switch_source.py 生成；粘贴进开发进度文档 T1.11 备注。",
            "",
        ]
        return "\n".join(lines)
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="T1.11 切源演练（实机）")
    parser.add_argument("--code", default=None, help="演练标的代码（默认取 stock_info 首只非退市）")
    parser.add_argument("--end", default=None, help="窗口结束日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--full", action="store_true", help="附带全量采集计时基线")
    parser.add_argument("--out", default=None, help="演练记录写入文件（默认打印到 stdout）")
    args = parser.parse_args()

    end = date.fromisoformat(args.end) if args.end else date.today()
    db = SessionLocal()
    try:
        code = args.code
        if not code:
            row = (
                db.query(StockInfo)
                .filter(StockInfo.is_delisted.is_(False))
                .order_by(StockInfo.code.asc())
                .first()
            )
            if row is None:
                raise SystemExit("stock_info 无标的，请先执行采集或用 --code 指定")
            code = row.code
    finally:
        db.close()

    report = run_drill(code, end, with_full=args.full)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"演练记录已写入 {args.out}")
    else:
        print(report)


if __name__ == "__main__":
    main()
