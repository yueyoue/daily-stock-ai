"""统一错误格式（开发文档 §7 约定）：{"code": <int>, "message": "<str>"}。"""
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger("app.error")


class AppError(Exception):
    """业务错误：code 采用 HTTP 语义状态码。"""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _envelope(code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=code if 400 <= code < 600 else 400,
                        content={"code": code, "message": message})


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def on_app_error(request: Request, exc: AppError):
        return _envelope(exc.code, exc.message)

    @app.exception_handler(HTTPException)
    async def on_http_error(request: Request, exc: HTTPException):
        return _envelope(exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        msg = f"参数校验失败：{first.get('loc', ['?'])[-1]} {first.get('msg', '')}".strip()
        return _envelope(400, msg)

    @app.exception_handler(Exception)
    async def on_unexpected(request: Request, exc: Exception):
        logger.exception("未处理异常: %s %s", request.url.path, exc)
        return _envelope(500, "服务器内部错误")
