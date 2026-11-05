"""T7.2 验收：盘前早报模板（7 维度硬约束 / 关键数值校验 / 结构化输入装配 / 重试 / 自检脚本）。

覆盖验收点（docs/开发进度文档.md T7.2）：
- 连续生成 3 篇均含 7 维度 → 假 LLM 连跑 3 个交易日，missing_sections 均为空、attempts==1
- 数值校验拦截错误值 → 字段缺失 / 数值不一致均进入 problems 并触发重试
"""
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app.core.errors import AppError
from app.models.base import SessionLocal, init_db
from app.models.content import HotTopic, News
from app.models.market import MarketDaily
from app.models.system import TaskLog, TradeCalendar
from app.services.report_morning import (
    NO_EVENT_SOURCE_NOTE,
    collect_morning_input,
    generate_morning,
    validate_sections,
    verify_key_numbers,
)
from app.services.trading_calendar import previous_trading_day, upsert_calendar

BACKEND = Path(__file__).resolve().parents[1]

# ---------- 测试夹具 ----------

SECTION_TITLES = (
    "## 1. 速读摘要",
    "## 2. 隔夜市场大事",
    "## 3. 市场情绪预判",
    "## 4. 资金动向",
    "## 5. 操作提醒 / 风险提示",
    "## 6. 今日观察方向",
    "## 7. 今日重要事件与数据日历",
)
REQUIRED_RE = re.compile(r"^- (.+?)：([-\d.]+)", re.M)


def _clean(db):
    for model in (News, HotTopic, MarketDaily, TaskLog, TradeCalendar):
        db.query(model).delete()
    db.commit()


def _seed_market(db, d, *, base=0):
    db.add(MarketDaily(
        date=d, up_count=3000 + base, down_count=1800 + base,
        limit_up_count=60 + base, limit_down_count=8 + base,
        sentiment_score=62.5 + base, main_moneyflow=12_300_000_000.0,
        extra={"最高连板": 5, "连板梯队": {"5板": 1, "3板": 3, "2板": 6}},
    ))
    db.commit()


def _seed_hot(db, d):
    db.add_all([
        HotTopic(date=d, title="消费复苏", heat="低", industry_chain="白酒·食品",
                 related_codes=["600519"]),
        HotTopic(date=d, title="算力基础设施", heat="高", industry_chain="光模块·服务器",
                 related_codes=["300308", "000977"]),
        HotTopic(date=d, title="半导体设备", heat="中", industry_chain="刻蚀·清洗设备",
                 related_codes=["688012"]),
    ])
    db.commit()


def _parse_required(prompt: str) -> dict:
    return {m.group(1): m.group(2) for m in REQUIRED_RE.finditer(prompt)}


def _valid_md(nums: dict) -> str:
    return "\n\n".join([
        SECTION_TITLES[0],
        f"隔夜外围平稳；市场情绪得分 {nums['市场情绪得分']}。",
        SECTION_TITLES[1],
        "隔夜美股收涨，商品与汇率波动有限。",
        SECTION_TITLES[2],
        f"市场情绪得分 {nums['市场情绪得分']}，赚钱效应尚可。",
        SECTION_TITLES[3],
        f"涨停家数 {nums['涨停家数']}，跌停家数 {nums['跌停家数']}，"
        f"上涨家数 {nums['上涨家数']}，下跌家数 {nums['下跌家数']}，"
        f"主力资金净流入 {nums['主力资金净流入']}。",
        SECTION_TITLES[4],
        "注意高位股回撤风险，控制仓位。",
        SECTION_TITLES[5],
        "关注主线板块与题材线索。",
        SECTION_TITLES[6],
        f"{NO_EVENT_SOURCE_NOTE}。",
    ])


def _template_llm(db_, prompt, **kwargs):
    """确定性模板 LLM：按提示词中的「必填字段」渲染 7 维度正文（离线可跑）。"""
    return _valid_md(_parse_required(prompt))


# ---------- 7 维度硬约束 ----------

def test_generate_morning_three_runs_all_seven_sections():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        days = [date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)]
        for i, d in enumerate(days):
            _seed_market(db, previous_trading_day(db, d), base=i)

        for d in days:
            res = generate_morning(db, d, llm=_template_llm)
            assert res["ok"] is True, res["problems"]
            assert res["missing_sections"] == []
            assert res["number_problems"] == []
            assert res["attempts"] == 1
            assert res["chars"] > 0
            assert validate_sections(res["md"]) == []

        logs = db.query(TaskLog).filter(TaskLog.task_name == "盘前早报").all()
        assert len(logs) == 3 and all(log.status == "success" for log in logs)
    finally:
        db.close()


def test_generate_morning_flags_missing_sections_and_retries():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        d = date(2026, 11, 5)
        _seed_market(db, previous_trading_day(db, d))
        prompts = []

        def fake(db_, prompt, **kwargs):
            prompts.append(prompt)
            nums = _parse_required(prompt)
            md = _valid_md(nums).replace(SECTION_TITLES[2], "")  # 去掉「市场情绪预判」
            return md

        res = generate_morning(db, d, llm=fake)
        assert res["ok"] is False
        assert res["missing_sections"] == ["市场情绪预判"]
        assert res["attempts"] == 2                       # 一次不合格即带 corrections 重试
        assert "缺少维度：市场情绪预判" in res["problems"]
        assert len(prompts) == 2
        assert "上一次输出不合格" not in prompts[0]
        assert "上一次输出不合格" in prompts[1]

        logs = db.query(TaskLog).filter(TaskLog.task_name == "盘前早报").all()
        assert len(logs) == 1 and logs[0].status == "failed"
    finally:
        db.close()


def test_generate_morning_llm_failure_propagates():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        d = date(2026, 11, 5)
        _seed_market(db, previous_trading_day(db, d))

        def boom(db_, prompt, **kwargs):
            raise AppError(503, "未配置 LLM API Key（请先在系统设置保存）")

        with pytest.raises(AppError):
            generate_morning(db, d, llm=boom)
    finally:
        db.close()


# ---------- 关键数值校验 ----------

def test_verify_key_numbers_intercepts_wrong_value():
    key = [{"label": "涨停家数", "value": 60.0, "unit": "家", "display": "60"}]
    md = "## 1. 速读摘要\n\n涨停家数 999 家，情绪偏弱。"
    problems = verify_key_numbers(md, key)
    assert len(problems) == 1 and "数值不一致" in problems[0]
    assert "999" in problems[0] and "60" in problems[0]


def test_verify_key_numbers_intercepts_missing_field():
    key = [{"label": "市场情绪得分", "value": 62.5, "unit": "分", "display": "62.5"}]
    problems = verify_key_numbers("## 1. 速读摘要\n\n今日情绪尚可。", key)
    assert len(problems) == 1 and "缺少字段" in problems[0]


def test_verify_key_numbers_accepts_alias_and_tolerance():
    key = [{"label": "主力资金净流入", "value": 123.0, "unit": "亿元", "display": "123.00"}]
    # 同义字段名 + 2% 容差内（123.9 vs 123.0，偏差 0.73%）
    assert verify_key_numbers("资金动向：主力净流入 123.9 亿元。", key) == []
    # 超出容差 → 拦截
    assert verify_key_numbers("资金动向：主力净流入 200.0 亿元。", key) != []


def test_generate_morning_intercepts_wrong_number_end_to_end():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        d = date(2026, 11, 5)
        _seed_market(db, previous_trading_day(db, d))

        def wrong(db_, prompt, **kwargs):
            nums = _parse_required(prompt)
            nums["涨停家数"] = "999"                       # 与结构化输入（60）不符
            return _valid_md(nums)

        res = generate_morning(db, d, llm=wrong)
        assert res["ok"] is False
        assert res["missing_sections"] == []
        assert any("数值不一致" in p for p in res["number_problems"])
    finally:
        db.close()


# ---------- 结构化输入装配 ----------

def test_collect_morning_input_assembly():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        d = date(2026, 11, 5)
        prev = previous_trading_day(db, d)
        assert prev == date(2026, 11, 4)                 # 日历缺失时按周一~周五兜底
        _seed_market(db, prev)
        _seed_hot(db, prev)
        db.add_all([
            News(published_at=datetime(2026, 11, 5, 6, 30), title="隔夜重磅政策",
                 content="X" * 300, source="财联社", impact_level="高", dedup_hash="m-1"),
            News(published_at=datetime(2026, 11, 5, 7, 30), title="个股日常公告",
                 content="c", source="门户", impact_level="低", dedup_hash="m-2"),
            News(published_at=datetime(2026, 11, 2, 10, 0), title="窗口外旧闻",
                 content="c", source="门户", impact_level="高", dedup_hash="m-3"),
        ])
        db.commit()

        payload = collect_morning_input(db, d)
        assert payload["trade_date"] == "2026-11-05"
        assert payload["prev_trade_date"] == "2026-11-04"
        assert payload["market"]["available"] is True
        assert payload["market"]["limit_up_count"] == 60

        labels = {s["label"]: s["display"] for s in payload["key_numbers"]}
        assert labels["涨停家数"] == "60" and labels["市场情绪得分"] == "62.5"
        assert labels["主力资金净流入"] == "123.00"

        # 隔夜窗口只含当日 06:00~08:00，且优先高/中影响等级 → 仅 1 条
        assert len(payload["overnight_news"]) == 1
        assert payload["overnight_news"][0]["impact_level"] == "高"
        assert payload["overnight_news"][0]["summary"] == "X" * 120   # 正文截断 120 字

        # 热点按热度排序：高 → 中 → 低
        assert [t["heat"] for t in payload["hot_topics"]] == ["高", "中", "低"]

        # 事件日历：无结构化源 → 空清单 + 显式缺口说明
        assert payload["events"]["items"] == []
        assert payload["events"]["note"] == NO_EVENT_SOURCE_NOTE
        assert "不得编造" in payload["events"]["note"]
    finally:
        db.close()


def test_overnight_news_falls_back_when_no_high_or_medium():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        d = date(2026, 11, 5)
        _seed_market(db, previous_trading_day(db, d))
        db.add(News(published_at=datetime(2026, 11, 5, 7, 0), title="低等级资讯",
                    content="c", source="门户", impact_level="低", dedup_hash="l-1"))
        db.commit()
        news = collect_morning_input(db, d)["overnight_news"]
        assert len(news) == 1 and news[0]["impact_level"] == "低"
    finally:
        db.close()


# ---------- 交易日历 ----------

def test_previous_trading_day_skips_holiday():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        d = date(2026, 11, 5)                            # 周四
        assert previous_trading_day(db, d) == date(2026, 11, 4)   # 无日历时按周一~周五

        upsert_calendar(db, [
            {"date": date(2026, 11, 4), "is_trading_day": False},  # 临时休市
            {"date": date(2026, 11, 3), "is_trading_day": True},
        ])
        assert previous_trading_day(db, d) == date(2026, 11, 3)
    finally:
        db.close()


# ---------- 自检脚本 ----------

def test_eval_script_offline_runs():
    """--offline：模板 LLM 连跑 3 篇验证 7 维度 + 负向对照，退出码 0（可进 CI）。"""
    proc = subprocess.run(
        [sys.executable, "scripts/eval_morning_report.py", "--offline"],
        cwd=str(BACKEND), capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(BACKEND), "PYTHONUTF8": "1"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "7 维度" in proc.stdout
    assert "负向对照" in proc.stdout