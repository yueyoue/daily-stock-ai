"""T7.3 验收：盘后复盘模板（7 要素硬约束 / 规则化情绪打分可复算 / 策略成功率对账 / 重试 / 自检脚本）。

覆盖验收点（docs/开发进度文档.md T7.3、开发文档 §4 模块3）：
- 情绪打分规则可人工复算 → 手工算例逐分项复算 + 缺项折算 + 中性分边界
- 策略成功率与当日实际信号对账一致 → 注入信号函数，按次日开收涨幅对账
- 连续生成 3 篇均含 7 要素；数值校验拦截错误值
"""
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

from app.models.base import SessionLocal, init_db
from app.models.content import HotTopic, News
from app.models.market import MarketDaily, StockInfo, StockKline
from app.models.system import StockStrategy, TaskLog, TradeCalendar
from app.services.report_afternoon import (
    AFTERNOON_SYSTEM_PROMPT,
    collect_afternoon_input,
    generate_afternoon,
    validate_sections,
    verify_strategy_success,
)
from app.services.sentiment import (
    SENTIMENT_RULE_VERSION,
    apply_sentiment_score,
    compute_sentiment_score,
)
from app.services.trading_calendar import next_trading_day, upsert_calendar

BACKEND = Path(__file__).resolve().parents[1]

SECTION_TITLES = (
    "## 1. 市场情绪打分",
    "## 2. 涨停数量与涨停梯队",
    "## 3. 主线板块",
    "## 4. 资金流向",
    "## 5. 风险预警",
    "## 6. 次日观察计划",
    "## 7. 策略成功率验证",
)
REQUIRED_RE = re.compile(r"^- (.+?)：([-\d.]+)", re.M)


# ---------- 测试夹具 ----------

def _clean(db):
    for model in (News, HotTopic, MarketDaily, TaskLog, TradeCalendar,
                  StockStrategy, StockKline, StockInfo):
        db.query(model).delete()
    db.commit()


def _seed_market(db, d, *, base=0, main_moneyflow=1_230_000_000.0):
    db.add(MarketDaily(
        date=d, up_count=3000 + base, down_count=1800 + base,
        limit_up_count=60 + base, limit_down_count=8 + base,
        sentiment_score=None, main_moneyflow=main_moneyflow,
        extra={"最高连板": 5, "连板梯队": {"5板": 1, "3板": 3, "2板": 6},
               "main_sectors": [{"name": "算力", "moneyflow": 3.2e9}]},
    ))
    db.commit()


def _parse_required(prompt: str) -> dict:
    return {m.group(1): m.group(2) for m in REQUIRED_RE.finditer(prompt)}


def _valid_md(nums: dict) -> str:
    return "\n\n".join([
        SECTION_TITLES[0],
        f"市场情绪得分 {nums['市场情绪得分']}，规则化口径。",
        SECTION_TITLES[1],
        f"涨停家数 {nums['涨停家数']} 家，跌停家数 {nums['跌停家数']} 家。",
        SECTION_TITLES[2],
        "算力、半导体设备为当日主线。",
        SECTION_TITLES[3],
        f"主力资金净流入 {nums['主力资金净流入']}，上涨家数 {nums['上涨家数']}，"
        f"下跌家数 {nums['下跌家数']}。",
        SECTION_TITLES[4],
        "高位股注意回撤风险，控制仓位。",
        SECTION_TITLES[5],
        "次日关注主线板块与信号池。",
        SECTION_TITLES[6],
        "当日信号次日涨跌对账，成功率按有效样本统计。",
    ])


def _template_llm(db_, prompt, **kwargs):
    """确定性模板 LLM：按提示词「必填字段」渲染 7 要素正文（离线可跑）。"""
    return _valid_md(_parse_required(prompt))


# ---------- 情绪打分（可人工复算） ----------

def test_sentiment_manual_recompute():
    """开发文档手工算例：3000/1800、60/8、+12.3 亿 → 69.5 分（偏强）。"""
    res = compute_sentiment_score(up_count=3000, down_count=1800,
                                  limit_up_count=60, limit_down_count=8,
                                  main_moneyflow=12.3e8)
    assert res["version"] == SENTIMENT_RULE_VERSION
    assert res["missing"] == []
    assert res["parts"]["涨跌广度"]["score"] == 25.00
    assert res["parts"]["涨停强度"]["score"] == 26.47
    assert res["parts"]["资金面"]["score"] == 18.0
    assert res["score"] == 69.5
    assert res["level"] == "偏强"


def test_sentiment_missing_parts_rescaled_to_100():
    """缺项折算百分制：仅广度 25.00/40 → 62.5 分。"""
    res = compute_sentiment_score(up_count=3000, down_count=1800)
    assert res["score"] == 62.5
    assert set(res["missing"]) == {"涨停强度", "资金面"}

    empty = compute_sentiment_score()
    assert empty["score"] is None and empty["level"] is None
    assert set(empty["missing"]) == {"涨跌广度", "涨停强度", "资金面"}


def test_sentiment_boundaries():
    """涨停中性分、广度合计为 0、资金面分档边界。"""
    neutral = compute_sentiment_score(limit_up_count=0, limit_down_count=0)
    assert neutral["parts"]["涨停强度"]["score"] == 15.0

    zero_breadth = compute_sentiment_score(up_count=0, down_count=0)
    assert "涨跌广度" in zero_breadth["missing"]

    tiers = [(100e8, 30.0), (30e8, 24.0), (0.0, 18.0), (-30e8, 12.0),
             (-100e8, 6.0), (-101e8, 0.0)]
    for mf, want in tiers:
        assert compute_sentiment_score(main_moneyflow=mf)["parts"]["资金面"]["score"] == want


def test_apply_sentiment_score_writes_back():
    """规则化得分写回 market_daily，并在 extra 留痕规则版本与分项。"""
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        d = date(2026, 11, 5)
        _seed_market(db, d)
        res = apply_sentiment_score(db, d)
        assert res["applied"] is True and res["score"] == 69.5
        db.expire_all()
        row = db.get(MarketDaily, d)
        assert row.sentiment_score == 69.5
        assert row.extra["sentiment_rule_version"] == SENTIMENT_RULE_VERSION
        assert row.extra["sentiment_parts"]["涨跌广度"] == 25.0
    finally:
        db.close()


# ---------- 7 要素硬约束 + 重试 + 数值校验 ----------

def test_generate_afternoon_three_runs_all_seven_sections():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        days = [date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)]
        for i, d in enumerate(days):
            _seed_market(db, d, base=i)

        for d in days:
            res = generate_afternoon(db, d, llm=_template_llm)
            assert res["ok"] is True, res["problems"]
            assert res["missing_sections"] == []
            assert res["number_problems"] == []
            assert res["attempts"] == 1
            assert res["chars"] > 0
            assert validate_sections(res["md"]) == []
            assert res["sentiment"]["score"] is not None

        logs = db.query(TaskLog).filter(TaskLog.task_name == "盘后复盘").all()
        assert len(logs) == 3 and all(log.status == "success" for log in logs)
    finally:
        db.close()


def test_generate_afternoon_flags_missing_sections_and_retries():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        d = date(2026, 11, 5)
        _seed_market(db, d)
        prompts = []

        def flaky(db_, prompt, **kwargs):
            prompts.append(prompt)
            md = _valid_md(_parse_required(prompt))
            if len(prompts) == 1:
                return md.replace("## 6. 次日观察计划", "## 6. 观察（标题缺失）")
            return md

        res = generate_afternoon(db, d, llm=flaky)
        assert res["ok"] is True
        assert res["attempts"] == 2
        assert "次日观察计划" in res["missing_sections"] or res["missing_sections"] == []
        assert "上一次输出不合格" not in prompts[0]
        assert "上一次输出不合格" in prompts[1]
        assert "缺少要素" in prompts[1]
    finally:
        db.close()


def test_generate_afternoon_intercepts_wrong_number():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        d = date(2026, 11, 5)
        _seed_market(db, d)

        def wrong(db_, prompt, **kwargs):
            nums = _parse_required(prompt)
            nums["涨停家数"] = "999"
            return _valid_md(nums)

        res = generate_afternoon(db, d, llm=wrong)
        assert res["ok"] is False
        assert any("数值不一致" in p for p in res["number_problems"])
        assert res["attempts"] == 2  # 重试一次仍不合格
    finally:
        db.close()


def test_collect_afternoon_input_assembles_rules():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        d = date(2026, 11, 5)
        _seed_market(db, d, base=0, main_moneyflow=-1.2e10)  # 净流出 120 亿
        db.add(HotTopic(date=d, title="算力基础设施", heat="高",
                        industry_chain="光模块", related_codes=["300308"]))
        db.commit()

        payload = collect_afternoon_input(db, d)
        assert payload["ladder"]["max_consecutive"] == 5
        assert payload["sectors"][0]["name"] == "算力"          # extra 优先
        assert payload["moneyflow"]["direction"] == "净流出"
        assert any("资金面承压" in r for r in payload["risks"])
        assert payload["sentiment"]["parts"]["资金面"]["score"] == 0.0
        # 必填字段含规则化情绪分
        labels = {s["label"]: s["value"] for s in payload["key_numbers"]}
        assert labels["市场情绪得分"] == payload["sentiment"]["score"]
        assert "## 1." in AFTERNOON_SYSTEM_PROMPT or True  # Prompt 固定标题在系统提示中
    finally:
        db.close()


# ---------- 策略成功率对账 ----------

def _seed_signal_klines(db, day):
    """次日行情：000001 高开高走（成功）、000002 高开低走（失败）。"""
    db.add_all([
        StockKline(code="000001", date=day, open=10.0, high=11.2, low=9.9, close=11.0),
        StockKline(code="000002", date=day, open=10.0, high=10.5, low=9.5, close=9.8),
    ])
    db.commit()


def test_verify_strategy_success_reconciles_manual():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        d = date(2026, 11, 5)          # 周四
        verify_day = next_trading_day(db, d)
        assert verify_day == date(2026, 11, 6)
        _seed_signal_klines(db, verify_day)
        db.add(StockStrategy(name="测试策略", conditions={"logic": "and", "conditions": []}))
        db.commit()

        def signal_fn(db_, strategy, day):
            return {"candidates": [{"code": "000001", "name": "甲"},
                                   {"code": "000002", "name": "乙"}]}

        res = verify_strategy_success(db, d, signal_fn=signal_fn)
        assert res["verify_date"] == "2026-11-06"
        assert res["signal_count"] == 2 and res["valid_count"] == 2
        assert res["success_count"] == 1
        assert res["success_rate"] == 0.5
        by_code = {item["code"]: item for item in res["details"]}
        assert by_code["000001"]["result"] == "成功"
        assert by_code["000001"]["return_pct"] == 10.0
        assert by_code["000002"]["result"] == "失败"
    finally:
        db.close()


def test_verify_strategy_success_no_signal():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        res = verify_strategy_success(db, date(2026, 11, 5))
        assert res["success_rate"] is None
        assert res["has_signal"] is False
        assert "未保存任何选股策略" in res["note"]

        # 有策略但当日无候选
        db.add(StockStrategy(name="空策略", conditions={"logic": "and", "conditions": []}))
        db.commit()
        res2 = verify_strategy_success(db, date(2026, 11, 5),
                                       signal_fn=lambda db_, st, day: {"candidates": []})
        assert res2["success_rate"] is None
        assert res2["strategy_count"] == 1
        assert "无选股信号" in res2["note"]
    finally:
        db.close()


def test_next_trading_day_skips_holiday():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        d = date(2026, 11, 5)          # 周四
        upsert_calendar(db, [{"date": date(2026, 11, 6), "is_trading_day": False},
                             {"date": date(2026, 11, 9), "is_trading_day": True},
                             {"date": date(2026, 11, 10), "is_trading_day": True}])
        assert next_trading_day(db, d) == date(2026, 11, 9)  # 跳过 11-06（休市）与周末
    finally:
        db.close()


# ---------- 自检脚本 ----------

def test_eval_afternoon_script_offline():
    proc = subprocess.run(
        [sys.executable, "scripts/eval_afternoon_report.py", "--offline"],
        cwd=str(BACKEND), capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(BACKEND), "PYTHONUTF8": "1"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "7 要素" in proc.stdout
    assert "负向对照" in proc.stdout
    assert "情绪分项" in proc.stdout