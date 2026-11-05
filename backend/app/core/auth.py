"""登录鉴权（开发文档 §3.4）：全 API 鉴权中间件 + 会话 token + 登录限速 + 管理员种子。

- 会话 token 含密码指纹（哈希尾 12 位）：改密后旧会话自动失效
- 中间件兜底拦截全部 /api/*（白名单除外），新增路由自动被保护
"""
import secrets
import time

import bcrypt
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.models.admin_user import AdminUser
from app.models.base import get_db

COOKIE_NAME = "dsa_token"

# 鉴权白名单（开发文档 §3.4：仅 health / login）
_WHITELIST_BASE = {"/api/health", "/api/auth/login"}
# 接口文档白名单（T3.1 "OpenAPI 可浏览"）：仅 api_docs_enabled=True 时放行；
# 生产建议置 false（T8.5 安全自查），白名单收敛为仅 health/login，不再暴露接口清单
_WHITELIST_DOCS = {"/api/docs", "/api/openapi.json"}


def _whitelist() -> set[str]:
    """当前生效的免鉴权路径集合（随 API_DOCS_ENABLED 配置切换）。"""
    if get_settings().api_docs_enabled:
        return _WHITELIST_BASE | _WHITELIST_DOCS
    return set(_WHITELIST_BASE)


# ---------- 密码 ----------
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


# ---------- 会话 token ----------
def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="dsa-auth")


def create_token(username: str, password_hash: str) -> str:
    return _serializer().dumps({"u": username, "p": password_hash[-12:]})


def read_token(token: str) -> dict | None:
    try:
        data = _serializer().loads(
            token, max_age=get_settings().session_max_age_days * 86400
        )
    except (BadSignature, SignatureExpired):
        return None
    return data if isinstance(data, dict) else None


def get_token_from_request(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(COOKIE_NAME)


def require_user(request: Request, db: Session = Depends(get_db)) -> AdminUser:
    """接口级鉴权依赖：校验会话 + 账号存在 + 密码指纹。"""
    token = get_token_from_request(request)
    claims = read_token(token) if token else None
    if not claims:
        raise AppError(401, "未登录或会话已过期")
    user = db.query(AdminUser).filter(AdminUser.username == claims.get("u", "")).first()
    if not user or user.password_hash[-12:] != claims.get("p"):
        raise AppError(401, "会话已失效，请重新登录")
    return user


# ---------- 登录限速（开发文档 §3.4：5 次失败锁 10 分钟）----------
_fail_records: dict[str, dict] = {}


def check_login_lock(username: str) -> None:
    rec = _fail_records.get(username)
    if rec and rec.get("locked_until", 0) > time.time():
        minutes = int((rec["locked_until"] - time.time()) / 60) + 1
        raise AppError(429, f"登录失败次数过多，请 {minutes} 分钟后重试")


def record_login_failure(username: str) -> None:
    settings = get_settings()
    rec = _fail_records.setdefault(username, {"fails": 0, "locked_until": 0.0})
    rec["fails"] += 1
    if rec["fails"] >= settings.login_max_fails:
        rec["locked_until"] = time.time() + settings.login_lock_minutes * 60
        rec["fails"] = 0


def reset_login_failures(username: str) -> None:
    _fail_records.pop(username, None)


# ---------- 鉴权中间件（兜底：未登录访问任意 /api/* → 401）----------
def install_auth_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        if (
            path.startswith("/api/")
            and path not in _whitelist()
            and request.method != "OPTIONS"
        ):
            token = get_token_from_request(request)
            claims = read_token(token) if token else None
            if not claims:
                return JSONResponse(
                    status_code=401,
                    content={"code": 401, "message": "未登录或会话已过期"},
                )
        return await call_next(request)


# ---------- 管理员种子 ----------
def seed_admin(db: Session) -> None:
    from app.core.logging import get_logger

    settings = get_settings()
    if db.query(AdminUser).first():
        return
    generated = not settings.admin_initial_password
    password = settings.admin_initial_password or secrets.token_urlsafe(12)
    user = AdminUser(username=settings.admin_username, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    log = get_logger("app.auth")
    if generated:
        log.warning(
            "已创建管理员账号 %s，初始密码：%s（仅显示这一次，请登录后立即修改）",
            user.username,
            password,
        )
    else:
        log.warning("已创建管理员账号 %s（初始密码来自环境变量），请登录后修改", user.username)
