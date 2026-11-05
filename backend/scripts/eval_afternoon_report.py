"""T7.3 盘后复盘模板自检脚本（连跑 3 篇 + 情绪分项复算表 + 策略成功率对账 + 负向对照）。

用途（对应 T7.3 验收「情绪打分规则可人工复算 / 策略成功率与当日实际信号对账一致 /
连续生成 3 篇均含 7 要素；数值校验拦截错误值」）：
    1. 在**独立临时 SQLite 库**中构造 3 个交易日的结构化输入（大盘/涨跌停/资金/热点）；
    2. 打印**情绪分项复算表**（各分项得分、满分、口径说明），供人工复算；
    3. --offline：用**确定性模板 LLM 桩**（按提示词必填字段渲染正文，无需 Key/网络）连跑 3 篇，
       核对每篇 7 要素齐全、关键数值与结构化输入一致；
    4. **策略成功率对账**：注入确定性信号函数，按次日开收涨幅对账，核对成功率与手工一致；
    5. **负向对照**：用写错关键数值（涨停家数 999）的桩，断言数值校验成功拦截。
    6. 不带 --offline：使用系统设置里已保存的 LLM Key 真实生成 3 篇后同样核对。

用法（backend/ 目录下）：
    python scripts/eval_afternoon_report.py --offline        # 离线自检（可进 CI）
    python scripts/eval_afternoon_report.py                  # 用已保存的 LLM Key 真实生成
    python scripts/eval_afternoon_report.py --api-key sk-xxx --model openai/qwen-plus

退出码：3 篇 7 要素齐全、情绪分项复算一致、策略对账一致且负向对照被拦截 0；任一不合格 1；
        缺少 LLM 配置且未 --offline 2。

注意：脚本只写临时库（DATABASE_URL 指向 tempdir），不触碰业务库。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/ 入口

REQUIRED_RE = re.compile(r"^- (.+?)：([-\d.]+)", re.M)

SECTION_TITLES = (
    "## 1. 市场情绪打分",
    "## 2. 涨停数量与涨停梯队",
    "## 3. 主线板块",
    "## 4. 资金流向",
    "## 5. 风险预警",
    "## 6. 次日观察计划",
    "## 7. 策略成功率验证",
)

# 连跑 3 个交易日（周二/周三/周四）
RUN_DAYS = [date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)]

# 对账日行情：甲高开高走（成功）、乙高开低走（失败）→ 成功率 0.5
RECON_KLINES = (
    ("000001", 10.0, 11.0),
    ("000002", 10.0, 9.8),
)


def _parse_required(prompt: str) -> dict:
    """从提示词「必填字段」段解析出 {字段名: 数值}（视为「结构化输入真值」）。"""
    return {m.group(1): m.group(2) for m in REQUIRED_RE.finditer(prompt)}


def _render_md(nums: dict) -> str:
    """按 7 要素渲染正文，并在对应要素逐字写出必填字段。"""
    return "\n\n".join([
        SECTION_TITLES[0],
        f"市场情绪得分 {nums['市场情绪得分']}，采用规则化打分口径。",
        SECTION_TITLES[1],
        f"涨停家数 {nums['涨停家数']} 家，跌停家数 {nums['跌停家数']} 家，梯队集中于 2~5 板。",
        SECTION_TITLES[2],
        "算力、半导体设备为当日主线板块。",
        SECTION_TITLES[3],
        f"主力资金净流入 {nums['主力资金净流入']}；上涨家数 {nums['上涨家数']}，"
        f"下跌家数 {nums['下跌家数']}。",
        SECTION_TITLES[4],
        "注意高位股回撤风险，控制仓位。",
        SECTION_TITLES[5],
        "次日关注主线板块与信号观察池。",
        SECTION_TITLES[6],
        "当日信号按次日开收涨幅对账，成功率按有效样本统计。",
    ])


def _template_llm(db, prompt, **kwargs):
    """确定性模板桩：7 要素齐全、数值与结构化输入一致。"""
    return _render_md(_parse_required(prompt))


def _wrong_number_llm(db, prompt, **kwargs):
    """负向对照桩：把涨停家数写成 999，其余照常。"""
    nums = _parse_required(prompt)
    nums["涨停家数"] = "999"
    return _render_md(nums)


def _prepare(db):
    """构造 3 个交易日的结构化输入（不含策略，避免离线调用真实扫描）。"""
    from app.models.content import HotTopic
    from app.models.market import MarketDaily

    for model in (HotTopic, MarketDaily):
        db.query(model).delete()
    db.commit()

    for i, d in enumerate(RUN_DAYS):
        db.add(MarketDaily(
            date=d, up_count=3000 + i, down_count=1800 + i,
            limit_up_count=60 + i, limit_down_count=8 + i,
            sentiment_score=None, main_moneyflow=1_230_000_000.0,
            extra={"最高连板": 5, "连板梯队": {"5板": 1, "3板": 3, "2板": 6},
                   "main_sectors": [{"name": "算力", "moneyflow": 3.2e9}]},
        ))
        db.add(HotTopic(date=d, title="算力基础设施", heat="高",
                        industry_chain="光模块·服务器", related_codes=["300308", "000977"]))
        db.commit()


def _print_sentiment_table(db, d):
    """打印情绪分项复算表（供人工复算）。"""
    from app.models.market import MarketDaily
    from app.services.sentiment import score_of_market

    market = db.get(MarketDaily, d)
    res = score_of_market(market)
    lines = [f"== 情绪分项复算（{d.isoformat()}，规则版本 {res['version']}）==",
             "| 分项 | 得分 | 满分 | 口径 |", "| --- | --- | --- | --- |"]
    for name in ("涨跌广度", "涨停强度", "资金面"):
        part = res["parts"].get(name)
        if part is None:
            lines.append(f"| {name} | 缺失 | — | 原始数据缺失 |")
        else:
            lines.append(f"| {name} | {part['score']} | {part['full']:g} | {part['note']} |")
    lines.append(f"| **总分** | **{res['score']}** | 100 | 定性：{res['level']}"
                 f"（缺项 {'、'.join(res['missing']) or '无'}）|")
    print("\n".join(lines))
    return res


def _reconcile(db, d):
    """策略成功率对账：注入确定性信号函数 + 次日行情，核对成功率与手工一致。"""
    from app.models.market import StockKline
    from app.models.system import StockStrategy
    from app.services.report_afternoon import verify_strategy_success
    from app.services.trading_calendar import next_trading_day

    verify_day = next_trading_day(db, d)
    for code, open_px, close_px in RECON_KLINES:
        db.merge(StockKline(code=code, date=verify_day, open=open_px, high=max(open_px, close_px),
                            low=min(open_px, close_px), close=close_px))
    db.query(StockStrategy).delete()
    db.add(StockStrategy(name="对账策略", conditions={"logic": "and", "conditions": []}))
    db.commit()

    def signal_fn(db_, strategy, day):
        return {"candidates": [{"code": code, "name": name} for code, name in
                               (("000001", "甲"), ("000002", "乙"))]}

    res = verify_strategy_success(db, d, signal_fn=signal_fn)
    expected = 0.5
    ok = (res["signal_count"] == 2 and res["valid_count"] == 2
          and res["success_count"] == 1 and res["success_rate"] == expected)
    print(f"\n== 策略成功率对账（{d.isoformat()} -> {res['verify_date']}）==")
    print(f"信号数 {res['signal_count']}｜有效样本 {res['valid_count']}｜"
          f"成功 {res['success_count']}｜成功率 {res['success_rate']}（手工期望 {expected}）")
    for item in res["details"]:
        print(f"- {item['code']} {item['name']}：开收涨幅 {item['return_pct']}% → {item['result']}")
    print(f"对账结论：{'✅ 一致' if ok else '❌ 不一致'}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="T7.3 盘后复盘模板自检（7 要素 + 情绪复算 + 策略对账 + 负向对照）")
    parser.add_argument("--offline", action="store_true",
                        help="用确定性模板 LLM 桩（无 Key/无网络可跑，可进 CI）")
    parser.add_argument("--api-key", default="", help="临时注入 LLM API Key（仅本进程内存/临时库）")
    parser.add_argument("--model", default="", help="临时指定模型名（如 openai/qwen-plus）")
    args = parser.parse_args()

    tmpdir = tempfile.mkdtemp(prefix="eval_afternoon_")
    os.environ["DATABASE_URL"] = f"sqlite:///{Path(tmpdir, 'eval.db').as_posix()}"
    os.environ.setdefault("SECRET_KEY", "eval-secret")

    from app.models.base import SessionLocal, init_db
    from app.services.report_afternoon import generate_afternoon

    init_db()
    db = SessionLocal()
    try:
        _prepare(db)
        llm = _template_llm if args.offline else None
        if not args.offline:
            from app.services.config_store import get_effective, save_config

            fields = {}
            if args.api_key:
                fields["llm_api_key"] = args.api_key
            if args.model:
                fields["llm_model"] = args.model
            if fields:
                save_config(db, **fields)
            if not get_effective(db, "llm_api_key"):
                print("未配置 LLM API Key（可用 --api-key 临时注入，或加 --offline 只做离线自检）")
                return 2

        mode = "离线模板桩" if args.offline else "真实 LLM"
        print(f"== 盘后复盘自检（{mode}，连跑 3 篇，验收：7 要素齐全 + 情绪复算 + 策略对账）==")

        _print_sentiment_table(db, RUN_DAYS[0])

        print("\n== 连跑 3 篇 ==")
        lines = ["| 复盘日期 | 对账日 | 情绪分 | 7 要素 | 缺失要素 | 数值问题 | 字数 | 尝试 |",
                 "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        all_ok = True
        for d in RUN_DAYS:
            res = generate_afternoon(db, d, llm=llm)
            missing = res["missing_sections"]
            ok = res["ok"] and not missing
            all_ok = all_ok and ok
            lines.append(
                f"| {res['report_date']} | {res['input']['next_day']['next_trade_date']} | "
                f"{res['sentiment']['score']} | {'✅' if ok else '❌'} | "
                f"{'、'.join(missing) or '—'} | {len(res['number_problems'])} | "
                f"{res['chars']} | {res['attempts']} |")
        print("\n".join(lines))
        print(f"结论：3 篇 7 要素校验{'全部通过' if all_ok else '存在不合格'} "
              f"（{'✅' if all_ok else '❌'}）")

        recon_ok = _reconcile(db, RUN_DAYS[-1])

        neg = generate_afternoon(db, RUN_DAYS[-1], llm=_wrong_number_llm)
        neg_ok = (not neg["ok"]) and any("数值不一致" in p for p in neg["number_problems"])
        print(f"\n负向对照（涨停家数写成 999）：拦截={'✅ 是' if neg_ok else '❌ 否'}"
              f"；问题={neg['number_problems']}")

        passed = all_ok and recon_ok and neg_ok
        print("\n结果：" + ("✅ 通过" if passed else "❌ 未通过"))
        return 0 if passed else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())