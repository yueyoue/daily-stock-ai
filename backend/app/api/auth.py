"""认证接口（开发文档 §7）：login / session / logout / password。"""
from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.core.auth import (
    COOKIE_NAME,
    check_login_lock,
    create_token,
    hash_password,
    record_login_failure,
    require_user,
    reset_login_failures,
    verify_password,
)
from app.core.config import get_settings
from app.core.errors import AppError
from app.models.admin_user import AdminUser
from app.models.base import get_db
from app.schemas.auth import LoginRequest, PasswordChangeRequest

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)):
    username = payload.username.strip()
    check_login_lock(username)
    user = db.query(AdminUser).filter(AdminUser.username == username).first()
    if not user or not verify_password(payload.password, user.password_hash):
        record_login_failure(username)
        raise AppError(401, "用户名或密码错误")
    reset_login_failures(username)
    token = create_token(user.username, user.password_hash)
    settings = get_settings()
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=settings.session_max_age_days * 86400,
        path="/",
    )
    # token 同时回传，便于脚本用 Authorization: Bearer 调试
    return {"username": user.username, "token": token}


@router.get("/session")
def session(user: AdminUser = Depends(require_user)):
    return {"username": user.username}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/password")
def change_password(
    payload: PasswordChangeRequest,
    user: AdminUser = Depends(require_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.old_password, user.password_hash):
        raise AppError(400, "原密码错误")
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"ok": True}
