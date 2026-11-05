"""T3.5 验收：配置接口（Key 掩码只进不出、范围校验、覆盖层生效）。"""
import os
import sys
import types

import pytest

from app.core.errors import AppError
from app.models.base import SessionLocal, init_db
from app.models.config import SystemConfig
from app.services.config_store import save_config
from app.services.indicator_store import store_mode_enabled


def _login(client):
    r = client.post("/api/auth/login",
                    json={"username": "admin",
                          "password": os.environ["ADMIN_INITIAL_PASSWORD"]})
    assert r.status_code == 200


def _clean(db):
    db.query(SystemConfig).delete()
    db.commit()


def teardown_function():
    """用例共享同一 DB：清理本模块写入的配置，避免污染后续模块（如指标模式用例）。"""
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
    finally:
        db.close()


def test_config_masks_llm_key_everywhere(client):
    init_db()
    _login(client)
    r = client.post("/api/config/save", json={"llm_api_key": "sk-test-12345678abcd"})
    assert r.status_code == 200
    assert "sk-test" not in r.text  # 任何位置无 Key 明文
    assert r.json()["llm_api_key_masked"] == "****abcd"  # 尾号 4 位

    r2 = client.get("/api/config/get")
    assert "sk-test" not in r2.text
    assert r2.json()["llm_api_key_masked"] == "****abcd"
    assert "llm_api_key" not in r2.json()  # 无明文字段


def test_empty_key_keeps_previous(client):
    init_db()
    _login(client)
    client.post("/api/config/save", json={"llm_api_key": "sk-keep-9999"})
    r = client.post("/api/config/save", json={"llm_api_key": ""})  # 空 = 不修改
    assert r.json()["llm_api_key_masked"] == "****9999"


def test_config_save_values_and_validation(client):
    init_db()
    _login(client)
    r = client.post("/api/config/save", json={
        "tech_indicator_store": True,
        "indicator_cache_days": 5,
        "indicator_engine": "pandas",
        "report_am_cron": "30 7 * * mon-fri",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["tech_indicator_store"] is True
    assert data["indicator_cache_days"] == 5
    assert data["indicator_engine"] == "pandas"
    assert data["report_am_cron"] == "30 7 * * mon-fri"

    # 范围 / 枚举 / 非法 cron → 400 统一错误格式
    for bad in ({"indicator_cache_days": 0},
                {"indicator_cache_days": 99},
                {"indicator_engine": "magic"},
                {"report_am_cron": "bad;cron"}):
        r2 = client.post("/api/config/save", json=bad)
        assert r2.status_code == 400
        assert r2.json()["code"] == 400


def test_save_config_rejects_unknown_key():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        with pytest.raises(AppError):
            save_config(db, unknown_key=1)
    finally:
        db.close()


def test_override_takes_effect_for_store_mode():
    init_db()
    db = SessionLocal()
    try:
        _clean(db)
        assert store_mode_enabled(db) is False  # 默认关（模式 B）
        save_config(db, tech_indicator_store=True)
        assert store_mode_enabled(db) is True   # 覆盖层生效（配置页切换）
        assert store_mode_enabled() is False    # 无 db 仍读环境默认
    finally:
        db.close()


# ---------- LLM 连通测试（T4.9） ----------

def test_test_llm_requires_key(client):
    init_db()
    _login(client)
    r = client.post("/api/config/test_llm")
    assert r.status_code == 400 and "API Key" in r.json()["message"]


def test_test_llm_ok_and_error_scrubbed(client, monkeypatch):
    init_db()
    _login(client)
    secret = "sk-secret-9876aaaa"
    client.post("/api/config/save", json={"llm_api_key": secret, "llm_model": "qwen-plus"})

    stub = types.ModuleType("litellm")

    def failing(**kw):
        raise RuntimeError(f"auth failed for {secret}")

    stub.completion = failing
    monkeypatch.setitem(sys.modules, "litellm", stub)
    r = client.post("/api/config/test_llm")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and "****" in body["error"]
    assert secret not in r.text  # 错误信息已脱敏，无 Key 明文

    stub.completion = lambda **kw: {"choices": []}
    r2 = client.post("/api/config/test_llm")
    assert r2.json() == {"ok": True, "model": "qwen-plus"}
