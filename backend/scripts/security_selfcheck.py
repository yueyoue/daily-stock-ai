"""T8.5 安全自查脚本（开发文档 §8.4 / §3.4）。

复现"安全自查清单"并给出可留档的证据（Markdown），供进度文档 T8.5 备注引用：
    A. 全 API 鉴权覆盖  —— 枚举 app 全部 /api/* 路由，未登录逐个试，除白名单外必须 401
    B. 白名单收敛        —— 免鉴权仅 /api/health + /api/auth/login；docs/openapi 随
                            API_DOCS_ENABLED 开关（生产 false 时同样 401）
    C. LLM Key 只进不出  —— 配置保存/读取响应均无明文，仅出尾号 4 位掩码
    D. 登录限速生效      —— 连续失败达阈值锁定（429）
    E. .env 不进 git 与镜像层 —— 未被跟踪、.gitignore 收录、.dockerignore 排除
    F. 无密钥泄露        —— 跟踪文件 + git 历史的疑似密钥模式扫描

用法（backend/ 目录下）：
    python scripts/security_selfcheck.py            # 打印 Markdown 清单
    python scripts/security_selfcheck.py --json      # 附机器可读结果

退出码：清单全过 0；关键项（A~E）任一失败或发现真实密钥 1。

注意：脚本只写临时库（DATABASE_URL 指向 tempdir），不触碰业务库。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/selfcheck.db")
os.environ.setdefault("SECRET_KEY", "selfcheck-secret")
os.environ.setdefault("ADMIN_INITIAL_PASSWORD", "selfcheck-pass-123")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from fastapi.testclient import TestClient  # noqa: E402

from app.core import auth as auth_core  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402

ADMIN_USER = "admin"
ADMIN_PASS = "selfcheck-pass-123"
PUBLIC_PATHS = {"/api/health", "/api/auth/login"}
DOCS_PATHS = {"/api/docs", "/api/openapi.json"}
_SWEEP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_SECRET_RE = re.compile(r"sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{12,}|-----BEGIN [A-Z ]*PRIVATE KEY-----")

results: list[dict] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append({"name": name, "ok": bool(ok), "detail": detail})


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
    return re.sub(r"\{[^}]+\}", "1", path)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)


def check_all_api_auth(client: TestClient) -> None:
    exempt = PUBLIC_PATHS | DOCS_PATHS
    routes = _api_routes()
    leaks: list[str] = []
    checked = 0
    for method, path in routes:
        concrete = _concrete(path)
        if concrete in exempt:
            continue
        resp = client.request(method, concrete)
        checked += 1
        if resp.status_code != 401 or resp.json().get("code") != 401:
            leaks.append(f"{method} {concrete} -> {resp.status_code}")
    record(
        "A 全 API 鉴权覆盖",
        not leaks and checked > 0,
        f"未登录逐个试 {checked} 条业务路由，越权放行 {len(leaks)} 条"
        + (f"：{leaks[:5]}" if leaks else "；全部 401 ✔"),
    )


def check_whitelist(client: TestClient) -> None:
    docs_on = get_settings().api_docs_enabled
    ok = client.get("/api/health").status_code == 200
    ok = ok and client.post("/api/auth/login", json={}).status_code == 400
    if docs_on:
        ok = ok and client.get("/api/docs").status_code == 200
        note = "docs/openapi 当前放行（API_DOCS_ENABLED=true，开发便利）→ 生产建议置 false"
    else:
        ok = ok and client.get("/api/docs").status_code == 401
        ok = ok and client.get("/api/openapi.json").status_code == 401
        note = "docs/openapi 已关闭（401），白名单收敛为仅 health/login ✔"
    record("B 白名单收敛", ok, f"免鉴权仅 health/login；{note}")


def check_key_masking(client: TestClient) -> None:
    from app.models.base import SessionLocal
    from app.models.config import SystemConfig

    client.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
    secret = "sk-live-abcdef1234567890"
    saved = client.post("/api/config/save", json={"llm_api_key": secret})
    got = client.get("/api/config/get")
    ok = (
        secret not in saved.text
        and secret not in got.text
        and "llm_api_key" not in saved.json()
        and "llm_api_key" not in got.json()
        and saved.json().get("llm_api_key_masked") == "****7890"
        and got.json().get("llm_api_key_masked") == "****7890"
    )
    record("C LLM Key 只进不出", ok, "保存/读取响应均无明文，仅掩码 ****7890（尾 4 位）")

    db = SessionLocal()
    try:
        row = db.get(SystemConfig, "llm_api_key")
        if row is not None:
            db.delete(row)
            db.commit()
    finally:
        db.close()


def check_login_lock(client: TestClient) -> None:
    user = "selfcheck-attacker"
    auth_core.reset_login_failures(user)
    try:
        last = None
        for _ in range(get_settings().login_max_fails):
            last = client.post("/api/auth/login", json={"username": user, "password": "x"})
        locked = client.post("/api/auth/login", json={"username": user, "password": "x"})
        ok = (
            last is not None
            and last.status_code == 401
            and locked.status_code == 429
        )
        record(
            "D 登录限速生效",
            ok,
            f"连续 {get_settings().login_max_fails} 次失败后第 "
            f"{get_settings().login_max_fails + 1} 次 -> {locked.status_code}",
        )
    finally:
        auth_core.reset_login_failures(user)


def check_env_hygiene() -> None:
    tracked = _git("ls-files", "--", ".env").stdout.strip()
    added = [
        ln
        for ln in _git("log", "--all", "--diff-filter=A", "--name-only", "--pretty=format:").stdout.splitlines()
        if ln.strip().endswith(".env")
    ]
    gitignore = {ln.strip() for ln in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    dockerignore_file = ROOT / ".dockerignore"
    docker_rules = set()
    if dockerignore_file.exists():
        docker_rules = {
            ln.strip()
            for ln in dockerignore_file.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        }
    ok = (
        not tracked
        and not added
        and ".env" in gitignore
        and ".env" in docker_rules
    )
    record(
        "E .env 不进 git 与镜像层",
        ok,
        f"git 跟踪={bool(tracked)}；历史新增={len(added)}；.gitignore 收录={'.env' in gitignore}；"
        f".dockerignore 排除={'.env' in docker_rules}",
    )


def check_secret_leak() -> None:
    hits: list[str] = []
    grep = _git("grep", "-nI", "-E", _SECRET_RE.pattern)
    for ln in grep.stdout.splitlines():
        if "xxxx" in ln or "example" in ln.lower():
            continue  # .env.example 占位符
        hits.append(ln)
    hist = _git("log", "--all", "-p", "-n", "200")
    for ln in hist.stdout.splitlines():
        m = _SECRET_RE.search(ln)
        if m and "xxxx" not in ln:
            hits.append("history: " + ln.strip()[:120])
    record("F 无密钥泄露", not hits, f"跟踪文件+近 200 提交扫描，命中 {len(hits)} 处")


def main() -> int:
    with TestClient(app) as client:
        check_all_api_auth(client)
        check_whitelist(client)
        check_key_masking(client)
        check_login_lock(client)
    check_env_hygiene()
    check_secret_leak()

    print("# T8.5 安全自查结果\n")
    print("| 检查项 | 结论 | 证据 |")
    print("|---|---|---|")
    for r in results:
        print(f"| {r['name']} | {'✅ 通过' if r['ok'] else '❌ 未过'} | {r['detail']} |")

    failed = [r["name"] for r in results if not r["ok"]]
    if "--json" in sys.argv:
        print("\n```json")
        print(json.dumps(results, ensure_ascii=False, indent=2))
        print("```")
    if failed:
        print(f"\n未通过项：{failed}")
        return 1
    print("\n清单全过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())