"""T6.2 验收：资讯降噪分级（LLM 输出映射 / 列表降噪过滤 / 失败兜底不丢数据 / 自检脚本）。

覆盖验收点：
- 抽 20 条人工评估分级合理性 → scripts/eval_news_classify.py 提供人工评估表格（真实 LLM 需 Key，
  本地/CI 无 Key 时用 --offline 验证「不丢数据」兜底；分级口径的确定性映射由本文件用假 LLM 覆盖）
- LLM 不可用时不丢数据 → 全部保持「未分级」原样、条数不变、只记 task_log
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from app.core.errors import AppError
from app.models.base import SessionLocal, init_db
from app.models.content import News
from app.models.system import TaskLog
from app.services.news_classify import (
    IRRELEVANT,
    UNCLASSIFIED,
    classify_pending,
    parse_levels,
)

BACKEND = Path(__file__).resolve().parents[1]


# ---------- 输出解析 ----------

def test_parse_levels_tolerates_markdown_and_bad_items():
    payload = json.dumps([
        {"index": 0, "level": "高"},
        {"index": True, "level": "中"},          # bool 索引 → 丢弃（True == 1）
        {"index": 99, "level": "低"},            # 越界 → 丢弃
        {"index": 1, "level": "不存在的等级"},    # 非法等级 → 保持未分级
        "not-a-dict",                            # 非对象 → 跳过
        {"index": 2, "level": "无关"},
    ], ensure_ascii=False)
    raw = "```json\n" + payload + "\n```"
    assert parse_levels(raw, 3) == ["高", None, "无关"]
    # 无法解析 / 空输出 → 全 None（该批保持未分级）
    assert parse_levels("完全不是 JSON", 2) == [None, None]
    assert parse_levels("", 1) == [None]
    assert parse_levels('{"a": 1}', 2) == [None, None]


# ---------- 分级与兜底 ----------

def _clean(db):
    for model in (News, TaskLog):
        db.query(model).delete()
    db.commit()


def _add_news(db, n):
    # 发布时间递减：分级按「近期优先」排序，故 T0 落在首批第一个（index 与标题一一对应）
    base = datetime(2026, 11, 5, 8, 0)
    db.add_all([
        News(published_at=base - timedelta(minutes=i), title=f"T{i}", content=f"C{i}",
             source="单测", impact_level=UNCLASSIFIED, dedup_hash=f"news-{i}")
        for i in range(n)
    ])
    db.commit()


def test_classify_pending_maps_levels_and_marks_irrelevant():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        _add_news(db, 4)
        prompts = []

        def fake(db_, prompt, **kwargs):
            prompts.append(prompt)
            return json.dumps([
                {"index": 0, "level": "高"},
                {"index": 1, "level": "无关"},
                {"index": 2, "level": "低"},
                {"index": 3, "level": "中"},
            ], ensure_ascii=False)

        stats = classify_pending(db, limit=50, llm=fake)
        assert stats["pending"] == 4 and stats["batches"] == 1
        assert stats["failed_batches"] == 0 and stats["ok"] is True
        assert stats["classified"] == {"高": 1, "中": 1, "低": 1}
        assert stats["irrelevant"] == 1 and stats["unclassified"] == 0
        assert len(prompts) == 1 and "T0" in prompts[0]  # 提示词带上了标题

        levels = {r.title: r.impact_level for r in db.query(News).all()}
        assert levels == {"T0": "高", "T1": "无关", "T2": "低", "T3": "中"}

        logs = db.query(TaskLog).filter(TaskLog.task_name == "资讯分级").all()
        assert len(logs) == 1 and logs[0].status == "success"
        assert "无关=1" in logs[0].message
    finally:
        db.close()


def test_classify_pending_llm_failure_keeps_unclassified():
    """LLM 不可用 → 不抛异常、不丢数据、保持未分级，只记 task_log（failed）。"""
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        _add_news(db, 3)
        before = db.query(News).count()

        def boom(db_, prompt, **kwargs):
            raise AppError(503, "未配置 LLM API Key（请先在系统设置保存）")

        stats = classify_pending(db, limit=50, llm=boom)
        db.expire_all()
        rows = db.query(News).all()
        assert db.query(News).count() == before == 3         # 零删除
        assert [r.impact_level for r in rows] == ["", "", ""]  # 保持未分级
        assert stats["failed_batches"] == 1 and stats["ok"] is False
        assert stats["pending"] == 3 and stats["unclassified"] == 3
        assert "LLM API Key" in stats["last_error"]

        logs = db.query(TaskLog).filter(TaskLog.task_name == "资讯分级").all()
        assert len(logs) == 1 and logs[0].status == "failed"
    finally:
        db.close()


def test_classify_pending_partial_output_keeps_missing_unclassified():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        _add_news(db, 3)

        def partial(db_, prompt, **kwargs):
            return '[{"index": 0, "level": "高"}]'

        stats = classify_pending(db, limit=50, llm=partial)
        levels = {r.title: r.impact_level for r in db.query(News).all()}
        assert levels == {"T0": "高", "T1": "", "T2": ""}
        assert stats["classified"]["高"] == 1 and stats["unclassified"] == 2
        assert stats["ok"] is True  # 批次可解析不算失败，缺项按未分级处理
    finally:
        db.close()


def test_classify_pending_empty_returns_ok():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        stats = classify_pending(db, limit=50, llm=lambda *a, **k: "[]")
        assert stats["pending"] == 0 and stats["ok"] is True
        assert stats["batches"] == 0 and db.query(TaskLog).count() == 0
    finally:
        db.close()


def test_classify_pending_skips_already_classified():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        db.add(News(published_at=datetime(2026, 11, 5, 8, 0), title="已分级",
                    content="c", source="单测", impact_level="高", dedup_hash="done-1"))
        _add_news(db, 1)
        db.commit()
        stats = classify_pending(db, limit=50, llm=lambda *a, **k: '[{"index":0,"level":"低"}]')
        assert stats["pending"] == 1  # 已分级的资讯不重复消耗 LLM
    finally:
        db.close()


# ---------- 列表降噪过滤 ----------

def _login(client):
    r = client.post("/api/auth/login",
                    json={"username": "admin",
                          "password": os.environ["ADMIN_INITIAL_PASSWORD"]})
    assert r.status_code == 200


def test_news_list_hides_irrelevant_by_default(client):
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        db.add_all([
            News(published_at=datetime(2026, 11, 5, 8, 0), title="重大政策", content="c",
                 source="财联社", impact_level="高", dedup_hash="n-high"),
            News(published_at=datetime(2026, 11, 5, 8, 1), title="娱乐新闻", content="c",
                 source="门户", impact_level=IRRELEVANT, dedup_hash="n-irr"),
            News(published_at=datetime(2026, 11, 5, 8, 2), title="待分级", content="c",
                 source="门户", impact_level=UNCLASSIFIED, dedup_hash="n-none"),
        ])
        db.commit()
        _login(client)

        body = client.get("/api/news/list").json()
        assert body["total"] == 2  # 「无关」默认隐藏；未分级仍展示
        assert {i["title"] for i in body["items"]} == {"重大政策", "待分级"}

        irr = client.get("/api/news/list?impact_level=无关").json()
        assert irr["total"] == 1 and irr["items"][0]["title"] == "娱乐新闻"

        full = client.get("/api/news/list?only_relevant=false").json()
        assert full["total"] == 3

        # 显式指定等级时不受 only_relevant 影响
        high = client.get("/api/news/list?impact_level=高").json()
        assert high["total"] == 1 and high["items"][0]["title"] == "重大政策"
    finally:
        db.close()


# ---------- 分级接口 ----------

def test_classify_api_returns_stats(client, monkeypatch):
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        _add_news(db, 2)
    finally:
        db.close()

    def fake_complete(db_, prompt, *, system=None, max_tokens=0, temperature=0.0, **kw):
        return '[{"index": 0, "level": "高"}, {"index": 1, "level": "无关"}]'

    monkeypatch.setattr("app.services.news_classify.complete", fake_complete)
    _login(client)

    r = client.post("/api/news/classify?limit=50&batch_size=20")
    assert r.status_code == 200
    body = r.json()
    assert body["pending"] == 2 and body["ok"] is True
    assert body["classified"]["高"] == 1 and body["irrelevant"] == 1

    assert client.get("/api/news/list").json()["total"] == 1  # 无关被降噪隐藏
    assert client.get("/api/news/list?only_relevant=false").json()["total"] == 2


def test_classify_api_requires_auth(client):
    assert client.post("/api/news/classify").status_code == 401


# ---------- 自检脚本 ----------

def test_eval_script_offline_runs(tmp_path):
    """--offline：LLM 桩必然失败 → 全部保持未分级、零丢失、退出码 0（可进 CI）。"""
    proc = subprocess.run(
        [sys.executable, "scripts/eval_news_classify.py", "--offline"],
        cwd=str(BACKEND), capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(BACKEND), "PYTHONUTF8": "1"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "兜底验证" in proc.stdout
    assert "数据零丢失" in proc.stdout
    assert "20 条人工标注样本" in Path(BACKEND, "scripts", "eval_news_classify.py").read_text(
        encoding="utf-8")