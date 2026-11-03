"""统一错误格式（开发文档 §7 约定）：{"code": <int>, "message": "<str>"}。

code 采用 HTTP 语义状态码，全站错误码表见 ERROR_CODES（前端按 code 分类提示）。
"""
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("app.error")

# 错误码表（T3.1 落地，开发文档 §7）：新增错误码先在此登记再使用
ERROR_CODES: dict[int, str] = {
    400: "参数错误/校验失败",
    401: "未登录或会话过期",
    403: "无权限",
    404: "资源不存在",
    409: "状态冲突（重复提交等）",
    429: "请求过于频繁/登录锁定",
    500: "服务器内部错误",
    502: "数据源全部失败（外部接口不可用）",
    503: "服务不可用（依赖缺失/未就绪）",
    504: "处理超时（扫描/任务超预算）",
}


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

    @app.exception_handler(StarletteHTTPException)
    async def on_http_error(request: Request, exc: StarletteHTTPException):
        # 挂 Starlette 基类：连 FastAPI 默认 404（detail 信封）也统一为 {code,message}
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
