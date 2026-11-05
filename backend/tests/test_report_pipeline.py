"""T7.4 验收：报告生成流水线（结构化 → LLM → 校验重试 → 入库版本痕迹 → 降级）。

覆盖验收点（docs/开发进度文档.md T7.4、开发文档 §4 模块3）：
- 生成失败自动重试：模板层带问题清单重试（第一次不合格 → 第二次合格），
  重试用尽仍不合格 → 抛 502 且不写库不覆盖旧报告
- 重新生成覆盖旧记录并留版本痕迹：同类型同日期 version+1 + 刷新 generated_at
- LLM 未就绪（503）→ 结构化骨架降级 + task_log warning（接口不整体失败）
- /api/report/regenerate 接真：走 LLM 流水线，响应带 ok/attempts/fallback/problems
"""
import os
import re
from datetime import date

import pytest

from app.core.errors import AppError
from app.models.base import SessionLocal, init_db
from app.models.content import Report
from app.models.market import MarketDaily
from app.models.system import TaskLog, TradeCalendar
from app.services.report import (
    FALLBACK_NOTE,
    LLM_UNAVAILABLE_CODES,
    REPORT_TITLES,
    generate_and_store,
    job_generate_afternoon,
    job_generate_morning,
)
from app.services.report_afternoon import validate_sections as validate_afternoon
from app.services.report_morning import (
    NO_EVENT_SOURCE_NOTE,
    validate_sections as validate_morning,
)

D = date(2026, 11, 5)
PREV = date(2026, 11, 4)

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
REQUIRED_RE = re.compile(r"^- (.+?)：([-\d.]+)", re.M)


# ---------- 夹具 ----------

def _clean(db):
    # TradeCalendar 也一并清理：全量套件共享同一 SQLite 文件，其他用例写入的日历
    # 会改变 previous_trading_day 的基准日，需复位到"无日历 → 周一~周五兜底"基线。
    for model in (Report, TaskLog, MarketDaily, TradeCalendar):
        db.query(model).delete()
    db.commit()


def _seed_market(db, d, *, sentiment=62.5, main_moneyflow=1_230_000_000.0):
    db.add(MarketDaily(
        date=d, up_count=3000, down_count=1800,
        limit_up_count=60, limit_down_count=8,
        sentiment_score=sentiment, main_moneyflow=main_moneyflow,
        extra={"最高连板": 5, "连板梯队": {"5板": 1, "3板": 3, "2板": 6},
               "main_sectors": [{"name": "算力", "moneyflow": 3.2e9}]},
    ))
    db.commit()


def _parse_required(prompt: str) -> dict:
    return {m.group(1): m.group(2) for m in REQUIRED_RE.finditer(prompt)}


def _morning_md(nums: dict) -> str:
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


def _afternoon_md(nums: dict) -> str:
    return "\n\n".join([
        AFTERNOON_TITLES[0],
        f"市场情绪得分 {nums['市场情绪得分']}，规则化口径。",
        AFTERNOON_TITLES[1],
        f"涨停家数 {nums['涨停家数']} 家，跌停家数 {nums['跌停家数']} 家。",
        AFTERNOON_TITLES[2],
        "算力、半导体设备为当日主线。",
        AFTERNOON_TITLES[3],
        f"主力资金净流入 {nums['主力资金净流入']}，上涨家数 {nums['上涨家数']}，"
        f"下跌家数 {nums['下跌家数']}。",
        AFTERNOON_TITLES[4],
        "高位股注意回撤风险，控制仓位。",
        AFTERNOON_TITLES[5],
        "次日关注主线板块与信号池。",
        AFTERNOON_TITLES[6],
        "当日信号次日涨跌对账，成功率按有效样本统计。",
    ])


def _morning_llm(db_, prompt, **kwargs):
    return _morning_md(_parse_required(prompt))


def _afternoon_llm(db_, prompt, **kwargs):
    return _afternoon_md(_parse_required(prompt))


def _login(client):
    r = client.post("/api/auth/login",
                    json={"username": "admin",
                          "password": os.environ["ADMIN_INITIAL_PASSWORD"]})
    assert r.status_code == 200
    return r


# ---------- 流水线：生成 + 入库 + 版本痕迹 ----------

def test_pipeline_llm_generates_and_versions_report():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        _seed_market(db, PREV)

        first = generate_and_store(db, "morning", D, llm=_morning_llm)
        assert first["ok"] is True and first["fallback"] is False
        assert first["llm_used"] is True and first["attempts"] == 1
        assert first["problems"] == []
        row = first["report"]
        assert row.version == 1
        assert row.title == f"{D.isoformat()} {REPORT_TITLES['morning']}"
        assert validate_morning(row.md_content) == []      # 7 维度齐全
        assert "（结构化骨架" not in row.md_content          # 非降级输出
        first_at, first_id = row.generated_at, row.id

        second = generate_and_store(db, "morning", D, llm=_morning_llm)
        row2 = second["report"]
        assert row2.id == first_id and row2.version == 2    # 覆盖旧记录 + 版本 +1
        assert row2.generated_at >= first_at                # 生成时间刷新
        assert db.query(Report).count() == 1                # 不新增记录
    finally:
        db.close()


def test_pipeline_afternoon_writes_rule_sentiment_and_stores():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        _seed_market(db, D, sentiment=None)

        res = generate_and_store(db, "afternoon", D, llm=_afternoon_llm)
        md = res["report"].md_content
        assert res["report"].version == 1 and res["fallback"] is False
        assert validate_afternoon(md) == []                 # 7 要素齐全
        assert "策略成功率" in md and "涨停梯队" in md
        db.expire_all()
        market = db.get(MarketDaily, D)
        assert market.sentiment_score == 69.5               # 规则化情绪分写回（可复算）
        assert market.extra["sentiment_rule_version"] == "v1"
    finally:
        db.close()


# ---------- 失败自动重试 ----------

def test_pipeline_retry_recovers_on_second_attempt():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        _seed_market(db, PREV)
        prompts: list[str] = []

        def flaky(db_, prompt, **kwargs):
            prompts.append(prompt)
            if len(prompts) == 1:                            # 首次漏「市场情绪预判」
                return _morning_md(_parse_required(prompt)).replace(
                    MORNING_TITLES[2], "")
            return _morning_llm(db_, prompt, **kwargs)

        res = generate_and_store(db, "morning", D, llm=flaky)
        assert res["ok"] is True and res["attempts"] == 2
        assert validate_morning(res["report"].md_content) == []
        assert len(prompts) == 2
        assert "上一次输出不合格" not in prompts[0]
        assert "上一次输出不合格" in prompts[1]               # 带问题清单回灌重试
        assert res["report"].version == 1
    finally:
        db.close()


def test_pipeline_persistent_failure_raises_and_keeps_old_report():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        _seed_market(db, PREV)
        good = generate_and_store(db, "morning", D, llm=_morning_llm)
        old_md, old_version = good["report"].md_content, good["report"].version
        calls: list[str] = []

        def broken(db_, prompt, **kwargs):
            calls.append(prompt)
            return _morning_md(_parse_required(prompt)).replace(MORNING_TITLES[2], "")

        with pytest.raises(AppError) as exc:
            generate_and_store(db, "morning", D, llm=broken)
        assert exc.value.code == 502
        assert "已自动重试 2 次" in exc.value.message
        assert "缺少维度：市场情绪预判" in exc.value.message
        assert len(calls) == 2                              # 自动重试 2 次

        db.expire_all()
        rows = db.query(Report).all()
        assert len(rows) == 1                               # 不新增、不覆盖旧报告
        assert rows[0].version == old_version and rows[0].md_content == old_md
        assert db.query(TaskLog).filter(TaskLog.status == "failed").count() >= 1
    finally:
        db.close()


# ---------- LLM 未就绪 → 结构化骨架降级 ----------

def test_pipeline_falls_back_to_skeleton_when_llm_unavailable(monkeypatch):
    """llm 缺省时走 app.services.llm.complete；503 时降级骨架 + task_log warning。"""
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        _seed_market(db, PREV)
        import app.services.llm as llm_mod

        def unavailable(*args, **kwargs):
            raise AppError(503, "未配置 LLM API Key（请先在系统设置保存）")

        monkeypatch.setattr(llm_mod, "complete", unavailable)

        res = generate_and_store(db, "morning", D)          # 不传 llm → 真实网关
        assert res["fallback"] is True and res["llm_used"] is False
        assert res["attempts"] == 0
        assert "未配置 LLM API Key" in res["fallback_reason"]
        assert FALLBACK_NOTE in res["report"].md_content
        assert validate_morning(res["report"].md_content) == []   # 骨架仍覆盖 7 维度

        warn = (db.query(TaskLog)
                .filter(TaskLog.task_name == "盘前早报生成",
                        TaskLog.status == "warning").all())
        assert len(warn) == 1 and "降级结构化骨架" in warn[0].message
        assert 503 in LLM_UNAVAILABLE_CODES
    finally:
        db.close()


def test_pipeline_afternoon_fallback_applies_rule_sentiment():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        _seed_market(db, D, sentiment=None)

        def unavailable(*args, **kwargs):
            raise AppError(503, "LLM 依赖未安装（生产镜像内含 litellm）")

        res = generate_and_store(db, "afternoon", D, llm=unavailable)
        assert res["fallback"] is True
        md = res["report"].md_content
        assert validate_afternoon(md) == []
        db.expire_all()
        assert db.get(MarketDaily, D).sentiment_score == 69.5   # 降级路径同样落规则分
    finally:
        db.close()


# ---------- 定时任务入口 ----------

def test_job_entrypoints_use_pipeline(monkeypatch):
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        _seed_market(db, PREV)
        _seed_market(db, D, sentiment=None)
        import app.services.llm as llm_mod

        monkeypatch.setattr(llm_mod, "complete",
                            lambda db_, prompt, **kw: (
                                _afternoon_md if "复盘" in prompt else _morning_md)(
                                    _parse_required(prompt)))

        am = job_generate_morning(db, D)
        assert am["report_type"] == "morning" and am["fallback"] is False
        assert am["report"].version == 1

        pm = job_generate_afternoon(db, D)
        assert pm["report_type"] == "afternoon" and pm["fallback"] is False
        assert pm["attempts"] == 1
        assert db.query(Report).count() == 2               # 早报 + 复盘各一条
    finally:
        db.close()


# ---------- API 接真 ----------

def test_api_regenerate_runs_llm_pipeline(client, monkeypatch):
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        _seed_market(db, PREV)
        _login(client)
        import app.services.llm as llm_mod

        monkeypatch.setattr(llm_mod, "complete", _morning_llm)

        r = client.post("/api/report/regenerate",
                        json={"type": "morning", "report_date": D.isoformat()})
        assert r.status_code == 200
        body = r.json()
        assert body["version"] == 1 and body["ok"] is True
        assert body["fallback"] is False and body["llm_used"] is True
        assert body["attempts"] == 1 and body["problems"] == []

        md = client.get(f"/api/report/{body['id']}").json()["md_content"]
        assert validate_morning(md) == []

        r2 = client.post("/api/report/regenerate",
                         json={"type": "morning", "report_date": D.isoformat()})
        assert r2.json()["id"] == body["id"] and r2.json()["version"] == 2
    finally:
        db.close()


def test_api_regenerate_returns_502_when_generation_fails(client, monkeypatch):
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        _seed_market(db, PREV)
        _login(client)
        import app.services.llm as llm_mod

        def broken(db_, prompt, **kwargs):
            return _morning_md(_parse_required(prompt)).replace(MORNING_TITLES[2], "")

        monkeypatch.setattr(llm_mod, "complete", broken)

        r = client.post("/api/report/regenerate",
                        json={"type": "morning", "report_date": D.isoformat()})
        assert r.status_code == 502 and r.json()["code"] == 502
        assert "缺少维度" in r.json()["message"]
        assert client.get("/api/report/list?type=morning").json()["total"] == 0
    finally:
        db.close()


def test_api_regenerate_falls_back_without_key(client, monkeypatch):
    """未配置 Key（503）：接口仍 200，正文为结构化骨架并标记 fallback。"""
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        _seed_market(db, PREV)
        _login(client)
        import app.services.llm as llm_mod

        def unavailable(*args, **kwargs):
            raise AppError(503, "未配置 LLM API Key（请先在系统设置保存）")

        monkeypatch.setattr(llm_mod, "complete", unavailable)

        r = client.post("/api/report/regenerate",
                        json={"type": "morning", "report_date": D.isoformat()})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["fallback"] is True and body["llm_used"] is False
        md = client.get(f"/api/report/{body['id']}").json()["md_content"]
        assert validate_morning(md) == []
        assert client.post("/api/report/regenerate",
                           json={"type": "bad", "report_date": D.isoformat()}
                           ).json()["code"] == 400
    finally:
        db.close()