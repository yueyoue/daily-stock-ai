"""T7.1 验收：配置切换模型生效、Key 不出现在日志/响应、重试/用量记录。"""
import sys
import types

import pytest

from app.core.errors import AppError
from app.models.base import SessionLocal, init_db
from app.models.config import SystemConfig
from app.models.system import TaskLog
from app.services.config_store import save_config
from app.services.llm import complete, probe_llm

CALLS: list[dict] = []


class FakeResp(dict):
    pass


def _install_fake_llm(monkeypatch, results):
    """results: 每次调用返回值或抛出的异常，按序消费。"""
    CALLS.clear()
    fake = types.ModuleType("litellm")

    def completion(**kwargs):
        CALLS.append(kwargs)
        item = results[min(len(CALLS) - 1, len(results) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    fake.completion = completion
    monkeypatch.setitem(sys.modules, "litellm", fake)


def _ok_resp(content="生成内容"):
    return FakeResp(choices=[{"message": {"content": content}}], usage={"total_tokens": 42})


def _clean(db):
    db.query(TaskLog).delete()
    db.query(SystemConfig).delete()
    db.commit()


def setup_env(db):
    _clean(db)
    save_config(db, llm_api_key="sk-test-key-abcdef", llm_model="qwen-plus")


def test_complete_success_and_usage_log(monkeypatch):
    init_db()
    db = SessionLocal()
    try:
        setup_env(db)
        _install_fake_llm(monkeypatch, [_ok_resp("你好")])
        out = complete(db, "测试提示词")
        assert out == "你好"

        log = db.query(TaskLog).filter(TaskLog.task_name == "LLM调用").first()
        assert log is not None and log.status == "success"
        assert "usage=" in log.message              # 用量记录
        assert "sk-test-key" not in log.message      # Key 不入日志
    finally:
        _clean(db)
        db.close()


def test_model_switch_takes_effect(monkeypatch):
    init_db()
    db = SessionLocal()
    try:
        setup_env(db)
        save_config(db, llm_model="kimi-k2")        # 设置页切换模型
        _install_fake_llm(monkeypatch, [_ok_resp()])
        complete(db, "hi")
        assert CALLS[0]["model"] == "kimi-k2"        # 配置切换生效
    finally:
        _clean(db)
        db.close()


def test_retry_then_success(monkeypatch):
    init_db()
    db = SessionLocal()
    try:
        setup_env(db)
        sleeps = []
        _install_fake_llm(monkeypatch, [RuntimeError("临时故障"),
                                        RuntimeError("临时故障"),
                                        _ok_resp("恢复")])
        out = complete(db, "hi", sleep=lambda s: sleeps.append(s))
        assert out == "恢复"
        assert len(CALLS) == 3
        assert sleeps == [1.0, 2.0]                  # 指数退避
    finally:
        _clean(db)
        db.close()


def test_key_scrubbed_in_error_and_log(monkeypatch):
    init_db()
    db = SessionLocal()
    try:
        setup_env(db)
        _install_fake_llm(monkeypatch, [RuntimeError("auth failed for key sk-test-key-abcdef"),
                                        RuntimeError("auth failed for key sk-test-key-abcdef"),
                                        RuntimeError("auth failed for key sk-test-key-abcdef")])
        with pytest.raises(AppError) as ei:
            complete(db, "hi", sleep=lambda s: None)
        assert "sk-test-key-abcdef" not in ei.value.message   # 响应无 Key 明文
        assert "****" in ei.value.message

        log = db.query(TaskLog).filter(TaskLog.task_name == "LLM调用").first()
        assert "sk-test-key-abcdef" not in log.message        # 日志无 Key 明文
    finally:
        _clean(db)
        db.close()


def test_probe_requires_key():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        with pytest.raises(AppError):
            probe_llm(db)
    finally:
        db.close()
