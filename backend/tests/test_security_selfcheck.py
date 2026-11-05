"""T8.5 安全自查（开发文档 §3.4 登录鉴权 / §8.4 安全）。

回归固化三项可机械判定的约束：
    1. 全 API 鉴权覆盖：任一 /api/* 业务路由未登录 → 401（白名单仅 health/login）；
       接口文档 docs/openapi 仅在 API_DOCS_ENABLED=true 时放行，关闭后同样 401。
    2. LLM Key 只进不出：配置写入后响应/读取均不含明文，仅出尾号 4 位掩码。
    3. 登录限速：连续失败达阈值锁定（429）。
另附 .env 卫生的静态断言（.gitignore 收录、.dockerignore 排除，不进 git 与镜像层）。
"""
import re
from pathlib import Path

from app.core import auth as auth_core
from app.core.config import get_settings
from app.main import app

ADMIN_USER = "admin"
ADMIN_PASS = "test-pass-123"

# 免鉴权白名单（与 core/auth.py 一致）
PUBLIC_PATHS = {"/api/health", "/api/auth/login"}
DOCS_PATHS = {"/api/docs", "/api/openapi.json"}
_SWEEP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _api_routes() -> list[tuple[str, str]]:
    """枚举 app 中全部 /api/* 路由（方法 × 路径）。

    兼容新版 FastAPI/Starlette 路由重构：include_router 的产物以
    ``_IncludedRouter`` 包装挂到 ``app.routes``，不再摊平成 APIRoute，需
    递归展开 ``original_router``；另并上 OpenAPI paths 兜底，确保路由发现
    不因框架版本变化而静默为空（静默为空会让鉴权覆盖检查假通过）。
    """
    found: set[tuple[str, str]] = set()

    def walk(routes) -> None:
        for route in routes:
            sub = getattr(route, "original_router", None)
            if sub is not None:
                walk(getattr(sub, "routes", None) or [])
                continue
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", None) or set()
            if path.startswith("/api/"):
                for m in methods & _SWEEP_METHODS:
                    found.add((m, path))

    walk(app.routes)
    try:
        for path, ops in app.openapi().get("paths", {}).items():
            if not path.startswith("/api/"):
                continue
            for m in ops:
                mu = m.upper()
                if mu in _SWEEP_METHODS:
                    found.add((mu, path))
    except Exception:  # pragma: no cover - OpenAPI 生成失败不应阻断路由发现
        pass
    return sorted(found)


def _concrete(path: str) -> str:
    """路径参数替换为占位值，确保命中路由（而非 404）。"""
    return re.sub(r"\{[^}]+\}", "1", path)


# ---------- 1. 全 API 鉴权覆盖 ----------

def test_route_sweep_has_protected_endpoints():
    routes = _api_routes()
    assert routes, "未枚举到任何 /api 路由，路由发现失效"
    # 至少覆盖各业务前缀，防止路由发现被误改成空集
    prefixes = {p.split("/")[2] for _, p in routes}
    for expected in ("collect", "report", "selector", "backtest", "config", "datasource"):
        assert expected in prefixes, f"路由清单缺少业务前缀 {expected}"


def test_all_business_api_requires_auth(client):
    """逐个试：除白名单外，任何 /api/* 未登录一律 401。"""
    exempt = PUBLIC_PATHS | DOCS_PATHS
    checked = 0
    for method, path in _api_routes():
        concrete = _concrete(path)
        if concrete in exempt:
            continue
        r = client.request(method, concrete)
        assert r.status_code == 401, f"{method} {concrete} 未登录应 401，实际 {r.status_code}"
        assert r.json().get("code") == 401, f"{method} {concrete} 错误信封应为 code=401"
        checked += 1
    assert checked >= 20, f"实际校验路由数异常偏少（{checked}），疑似白名单过宽"


def test_whitelist_only_health_and_login_by_default(client):
    assert client.get("/api/health").status_code == 200
    # login 在白名单：未登录即达业务层（空体校验失败 = 400，而非中间件 401）
    r = client.post("/api/auth/login", json={})
    assert r.status_code == 400 and r.json()["code"] == 400


def test_docs_can_be_closed_in_production(client, monkeypatch):
    """API_DOCS_ENABLED=false 时，白名单收敛为仅 health/login，文档接口未登录 401。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "api_docs_enabled", False, raising=False)

    assert auth_core._whitelist() == PUBLIC_PATHS
    assert client.get("/api/health").status_code == 200
    assert client.post("/api/auth/login", json={}).status_code == 400
    assert client.get("/api/docs").status_code == 401
    assert client.get("/api/openapi.json").status_code == 401

    monkeypatch.undo()
    assert auth_core._whitelist() == PUBLIC_PATHS | DOCS_PATHS


# ---------- 2. LLM Key 只进不出 ----------

def test_llm_key_masked_and_never_plaintext(client):
    from app.models.base import SessionLocal
    from app.models.config import SystemConfig

    client.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
    secret = "sk-live-abcdef1234567890"

    r = client.post("/api/config/save", json={"llm_api_key": secret})
    assert r.status_code == 200
    body = r.json()
    assert secret not in r.text, "配置保存响应泄露了 Key 明文"
    assert "llm_api_key" not in body, "响应不应出现明文字段名 llm_api_key"
    assert body["llm_api_key_masked"] == "****7890"

    r2 = client.get("/api/config/get")
    assert secret not in r2.text, "配置读取响应泄露了 Key 明文"
    assert "llm_api_key" not in r2.json()
    assert r2.json()["llm_api_key_masked"] == "****7890"

    # 清理覆盖，避免影响其他用例
    db = SessionLocal()
    try:
        row = db.get(SystemConfig, "llm_api_key")
        if row is not None:
            db.delete(row)
            db.commit()
    finally:
        db.close()


# ---------- 3. 登录限速 ----------

def test_login_rate_limit_locks_after_threshold(client):
    user = "selfcheck-attacker"
    auth_core.reset_login_failures(user)
    try:
        for _ in range(get_settings().login_max_fails):
            r = client.post("/api/auth/login", json={"username": user, "password": "x"})
            assert r.status_code == 401
        r = client.post("/api/auth/login", json={"username": user, "password": "x"})
        assert r.status_code == 429, "达到失败阈值后应锁定（429）"
        assert "锁定" in r.json()["message"] or "重试" in r.json()["message"]
    finally:
        auth_core.reset_login_failures(user)


# ---------- 4. .env 卫生（静态） ----------

def test_env_excluded_from_git_and_image_context():
    root = Path(__file__).resolve().parents[2]
    gitignore = [ln.strip() for ln in (root / ".gitignore").read_text(encoding="utf-8").splitlines()]
    assert ".env" in gitignore, ".gitignore 必须收录 .env"

    dockerignore_path = root / ".dockerignore"
    assert dockerignore_path.exists(), "缺少 .dockerignore，.env/.git 会进入构建上下文"
    rules = [
        ln.strip()
        for ln in dockerignore_path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert ".env" in rules, ".dockerignore 必须排除 .env"
    assert ".git" in rules, ".dockerignore 应排除 .git"