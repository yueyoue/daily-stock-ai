"""T7.6 验收脚本：连续 3 个交易日自动出报告 + 定时时刻核对 + 非交易日负向对照。

验收口径（docs/开发进度文档.md T7.6 / 开发文档 §4.9 时间线）：
    「**连续 3 个交易日** 08:00/17:00 自动出报告、时间戳正确」

三段验证：
    A. 定时时刻核对：注册表解析出的 cron（早报/复盘）在指定交易日触发时刻必须是
       当日 08:00 / 17:00（用 APScheduler CronTrigger 真实推算，TZ=Asia/Shanghai）；
       并核对周末不触发（下一次触发落在周一 08:00）。
    B. 连跑 3 个交易日：走注册表统一入口 run_job（交易日前置判断 + 统一执行器），
       注入 run_date 模拟历史日期；核对每日各出 1 篇早报/复盘、report_date 正确、
       generated_at 非空且为本次运行写入（时间戳正确）、version=1、6 条 success 日志。
    C. 负向对照：非交易日（周六）不出报告、不调用任务本体、只留 1 条 skipped 日志。

用法（backend/ 目录下）：
    python scripts/eval_three_day_reports.py --offline     # 离线（确定性 LLM 桩，可进 CI）
    python scripts/eval_three_day_reports.py               # 用系统设置里已保存的 LLM Key 真实连跑

退出码：全部通过 0；任一断言失败 1；缺少 LLM 配置且未 --offline 2。

注意：脚本只写临时库（DATABASE_URL 指向 tempdir），不触碰业务库。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/ 入口

REQUIRED_RE = re.compile(r"^- (.+?)：([-\d.]+)", re.M)

MORNING_TITLES = (
    "## 1. 速读摘要",
    "## 2. 隔夜市场大事",
    "## 3. 市场情绪预判",
    "## 4. 资金动向",
    "## 5. 操作提醒 / 风险提示",
    "## 6. 今日观察方向",
    "## 7. 今日重要事件与数据日历",
)
AFTERNOON_TITLES = (
    "## 1. 市场情绪打分",
    "## 2. 涨停数量与涨停梯队",
    "## 3. 主线板块",
    "## 4. 资金流向",
    "## 5. 风险预警",
    "## 6. 次日观察计划",
    "## 7. 策略成功率验证",
)

# 连续 3 个交易日（周二/周三/周四）；无交易日历时按"周一~周五兜底"判定
RUN_DAYS = [date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)]
NON_TRADING = date(2026, 11, 7)          # 周六
SEED_DAYS = [date(2026, 11, 2), *RUN_DAYS]   # 大盘数据（含 11-03 的上一交易日 11-02）

TIMESTAMP_TOLERANCE = timedelta(minutes=10)  # "时间戳正确"=本次运行写入（钟差容忍）


# ---------- 确定性 LLM 桩（离线模式） ----------

def _parse_required(prompt: str) -> dict:
    """从提示词「必填字段」段解析 {字段名: 数值显示}，视为结构化输入真值。"""
    return {m.group(1): m.group(2) for m in REQUIRED_RE.finditer(prompt)}


def _render_morning(nums: dict) -> str:
    from app.services.report_morning import NO_EVENT_SOURCE_NOTE

    return "\n\n".join([
        MORNING_TITLES[0],
        f"隔夜外围平稳；市场情绪得分 {nums['市场情绪得分']}。",
        MORNING_TITLES[1],
        "隔夜美股收涨，商品与汇率波动有限。",
        MORNING_TITLES[2],
        f"市场情绪得分 {nums['市场情绪得分']}，赚钱效应尚可。",
        MORNING_TITLES[3],
        f"涨停家数 {nums['涨停家数']}，跌停家数 {nums['跌停家数']}，"
        f"上涨家数 {nums['上涨家数']}，下跌家数 {nums['下跌家数']}，"
        f"主力资金净流入 {nums['主力资金净流入']}。",
        MORNING_TITLES[4],
        "注意高位股回撤风险，控制仓位。",
        MORNING_TITLES[5],
        "关注主线板块与题材线索。",
        MORNING_TITLES[6],
        f"{NO_EVENT_SOURCE_NOTE}。",
    ])


def _render_afternoon(nums: dict) -> str:
    return "\n\n".join([
        AFTERNOON_TITLES[0],
        f"市场情绪得分 {nums['市场情绪得分']}，规则化口径（v1）。",
        AFTERNOON_TITLES[1],
        f"涨停家数 {nums['涨停家数']} 家，跌停家数 {nums['跌停家数']} 家；最高连板 5 板。",
        AFTERNOON_TITLES[2],
        "算力、半导体设备为当日主线。",
        AFTERNOON_TITLES[3],
        f"主力资金净流入 {nums['主力资金净流入']}，上涨家数 {nums['上涨家数']}，"
        f"下跌家数 {nums['下跌家数']}。",
        AFTERNOON_TITLES[4],
        "未触发规则化风险预警（阈值未达）。",
        AFTERNOON_TITLES[5],
        "次日关注主线板块与当日信号池。",
        AFTERNOON_TITLES[6],
        "当日无选股信号，策略成功率不适用（如实说明）。",
    ])


def _stub_llm(db, prompt, *, system="", **kwargs):
    """确定性桩：按提示词必填字段渲染正文（7 维度齐全、数值与结构化输入一致）。"""
    nums = _parse_required(prompt)
    return _render_afternoon(nums) if "盘后" in system else _render_morning(nums)


# ---------- 结构化输入种子 ----------

def _seed(db):
    from app.models.market import MarketDaily

    db.query(MarketDaily).delete()
    db.commit()
    for i, d in enumerate(SEED_DAYS):
        db.add(MarketDaily(
            date=d, up_count=3000 + i, down_count=1800 + i,
            limit_up_count=60 + i, limit_down_count=8 + i,
            sentiment_score=62.5 + i, main_moneyflow=1_230_000_000.0,
            extra={"最高连板": 5, "连板梯队": {"5板": 1, "3板": 3, "2板": 6},
                   "main_sectors": [{"name": "算力", "moneyflow": 3.2e9}]},
        ))
    db.commit()


# ---------- A. 定时时刻核对 ----------

def _fire_time(cron: str, day: date, at: time = time.min):
    from apscheduler.triggers.cron import CronTrigger

    trigger = CronTrigger.from_crontab(cron, timezone="Asia/Shanghai")
    tz = trigger.timezone
    naive = datetime.combine(day, at)
    # 兼容 pytz（有 localize）与 zoneinfo（无 localize，直接挂 tzinfo）
    now = tz.localize(naive) if hasattr(tz, "localize") else naive.replace(tzinfo=tz)
    return trigger.get_next_fire_time(None, now)


def _check_schedule(specs: dict) -> list[tuple[str, str, bool]]:
    """核对早报/复盘在交易日 08:00 / 17:00 触发，且周末不触发（下一次落周一）。

    含周一触发项：APScheduler 3.x 下 `1-5` 会被解读为周二~周六，本项用于防止该写法回归。
    """
    rows: list[tuple[str, str, bool]] = []
    monday = date(2026, 11, 2)
    for name, expect_hm in (("report_morning", "08:00"), ("report_afternoon", "17:00")):
        cron = specs[name]["cron"]
        hh, mm = expect_hm.split(":")
        after = time(int(hh) + 1, int(mm))  # 当日该时刻已过 → 应顺延到下一交易日

        def got(d: date, at: time = time.min):
            return _fire_time(cron, d, at).strftime("%Y-%m-%d %H:%M")

        rows.append((f"{name}（{cron}）周一触发",
                     f"{monday.isoformat()} {expect_hm}", got(monday) == f"{monday.isoformat()} {expect_hm}"))
        rows.append((f"{name} 交易日当日",
                     f"{RUN_DAYS[0].isoformat()} {expect_hm}",
                     got(RUN_DAYS[0]) == f"{RUN_DAYS[0].isoformat()} {expect_hm}"))
        rows.append((f"{name} 当日已过顺延次日",
                     f"{RUN_DAYS[1].isoformat()} {expect_hm}",
                     got(RUN_DAYS[0], after) == f"{RUN_DAYS[1].isoformat()} {expect_hm}"))
        # 周六：cron 含 mon-fri，下一次必须落在周一同时刻（周末不触发）
        rows.append((f"{name} 周六不触发", f"2026-11-09 {expect_hm}",
                     got(NON_TRADING) == f"2026-11-09 {expect_hm}"))
    return rows


# ---------- 主流程 ----------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="T7.6 三日验证（连续 3 个交易日自动出报告 + 时刻与时间戳核对 + 负向对照）")
    parser.add_argument("--offline", action="store_true",
                        help="用确定性 LLM 桩（无 Key/无网络可跑，可进 CI）")
    args = parser.parse_args()

    tmpdir = tempfile.mkdtemp(prefix="eval_three_day_")
    os.environ["DATABASE_URL"] = f"sqlite:///{Path(tmpdir, 'eval.db').as_posix()}"
    os.environ.setdefault("SECRET_KEY", "eval-secret")
    os.environ["SCHEDULER_ENABLED"] = "false"

    from app.models.base import SessionLocal, init_db
    from app.models.content import Report
    from app.models.system import TaskLog
    from app.scheduler.registry import get_job_specs, run_job
    from app.services.task_runner import run_task

    init_db()
    db = SessionLocal()
    try:
        _seed(db)
        llm = _stub_llm if args.offline else None
        if not args.offline:
            from app.services.config_store import get_effective

            if not get_effective(db, "llm_api_key"):
                print("未配置 LLM API Key（可加 --offline 走确定性桩做离线验证）")
                return 2

        specs = {s["name"]: s for s in get_job_specs(db)}
        results: list[bool] = []
        mode = "离线确定性桩" if args.offline else "真实 LLM"

        print(f"== T7.6 三日验证（{mode}，连续 3 个交易日："
              f"{'、'.join(d.isoformat() for d in RUN_DAYS)}）==\n")

        # A. 定时时刻核对
        print("A. 定时时刻核对（APScheduler CronTrigger，TZ=Asia/Shanghai）")
        print("| 核对项 | 期望 | 结果 |")
        print("| --- | --- | --- |")
        for item, expect, ok in _check_schedule(specs):
            results.append(ok)
            print(f"| {item} | {expect} | {'✅' if ok else '❌'} |")

        # B. 连跑 3 个交易日（注入 run_date 模拟历史日期）
        def make_runner(day: date):
            def runner(name, func):
                return run_task(
                    name,
                    lambda db_, **kw: func(db_, run_date=day),
                    sleep=lambda _s: None,
                )
            return runner

        print("\nB. 连续 3 个交易日自动出报告（run_job + 统一执行器）")
        print("| 报告日期 | 早报 | 复盘 | 报告时间戳 | 版本 | 日志 |")
        print("| --- | --- | --- | --- | --- | --- |")
        for d in RUN_DAYS:
            run_job(specs["report_morning"], today=d, db=db, runner=make_runner(d))
            run_job(specs["report_afternoon"], today=d, db=db, runner=make_runner(d))

            rows = db.query(Report).filter(Report.report_date == d).order_by(Report.id).all()
            am = [r for r in rows if r.type == "morning"]
            pm = [r for r in rows if r.type == "afternoon"]
            day_ok = len(am) == 1 and len(pm) == 1
            ts_ok = all(
                r.generated_at is not None
                and abs(datetime.now() - r.generated_at) <= TIMESTAMP_TOLERANCE
                for r in (am + pm)
            )
            ver_ok = all(r.version == 1 for r in (am + pm))
            logs_before = (db.query(TaskLog)
                           .filter(TaskLog.task_name.in_(("report_morning", "report_afternoon")),
                                   TaskLog.status == "success").count())
            # 顶层任务行：每个任务 1 条（task_runner 以 report_morning/report_afternoon 记 success）
            logs_ok = logs_before == (RUN_DAYS.index(d) + 1) * 2  # 每日新增 2 条（早报+复盘）
            results += [day_ok, ts_ok, ver_ok, logs_ok]
            print(f"| {d.isoformat()} | {'✅' if am else '❌'} | {'✅' if pm else '❌'} | "
                  f"{'✅' if ts_ok else '❌'}（{am[0].generated_at if am else '—'}） | "
                  f"{'✅' if ver_ok else '❌'} v{am[0].version if am else '—'} | "
                  f"{'✅' if logs_ok else '❌'} 累计 {logs_before} 条 |")

        # C. 负向对照：非交易日不出报告
        calls: list[str] = []
        before = db.query(Report).count()
        res = run_job(specs["report_morning"], today=NON_TRADING, db=db,
                      runner=lambda name, fn: calls.append(name))
        after = db.query(Report).count()
        skipped = (db.query(TaskLog)
                   .filter(TaskLog.task_name == "report_morning:跳过",
                           TaskLog.status == "skipped").count())
        neg_ok = (res["executed"] is False and res["reason"] == "non_trading_day"
                  and calls == [] and after == before and skipped == 1)
        results.append(neg_ok)
        print(f"\nC. 负向对照（{NON_TRADING.isoformat()} 周六）：executed={res['executed']}、"
              f"调用任务本体={len(calls)} 次、新增报告={after - before} 篇、"
              f"skipped 日志={skipped} 条 → {'✅' if neg_ok else '❌'}")

        passed = all(results)
        print(f"\n结果：{'✅ 通过' if passed else '❌ 未通过'}"
              f"（{sum(results)}/{len(results)} 项断言通过）")
        return 0 if passed else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())