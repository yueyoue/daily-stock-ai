"""DailyStockAI 后端入口（开发文档 §2.1：单镜像由 FastAPI 托管前端产物）。"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse

from app.api.auth import router as auth_router
from app.api.backtest import router as backtest_router
from app.api.collect import router as collect_router
from app.api.dashboard import router as dashboard_router
from app.api.config import router as config_router
from app.api.datasource import router as datasource_router
from app.api.deps import require_user
from app.api.health import router as health_router
from app.api.hot import router as hot_router
from app.api.news import router as news_router
from app.api.report import router as report_router
from app.api.selector import router as selector_router
from app.api.task import router as task_router
from app.api.watchlist import router as watchlist_router
from app.core.auth import install_auth_middleware, seed_admin
from app.core.config import get_settings
from app.core.errors import install_error_handlers
from app.core.logging import setup_logging
from app.services.source_seed import seed_data_sources

setup_logging()
logger = logging.getLogger("app.main")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.models.base import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    scheduler = None
    try:
        seed_admin(db)
        seed_data_sources(db)
        # T7.5 定时调度：TZ 自检 → 装配注册表 → 启动（起停随 lifespan）
        if settings.scheduler_enabled:
            from app.scheduler.registry import build_scheduler, tz_selfcheck

            tz_selfcheck()
            scheduler = build_scheduler(db)
            scheduler.start()
            app.state.scheduler = scheduler
            logger.info("定时调度已启动（TZ=%s，§4.9）", settings.timezone)
        else:
            logger.warning("定时调度未启用（SCHEDULER_ENABLED=false）")
    finally:
        db.close()
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
            logger.info("定时调度已停止")


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description=(
        "DailyStockAI API（开发文档 §7 契约）。约定："
        "① 鉴权覆盖全部 /api/*（免鉴权仅 health/login；接口文档 docs/openapi "
        "仅当 API_DOCS_ENABLED=true 时放行，生产建议关闭）；"
        "② 统一错误格式 {\"code\": int, \"message\": str}，错误码表见 core/errors.ERROR_CODES；"
        "③ 列表接口统一分页参数 page/page_size，响应 {items,total,page,page_size}。"
    ),
    docs_url="/api/docs" if settings.api_docs_enabled else None,
    openapi_url="/api/openapi.json" if settings.api_docs_enabled else None,
    lifespan=lifespan,
)

install_error_handlers(app)
install_auth_middleware(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- API 路由 ----
app.include_router(health_router)
app.include_router(auth_router)
# 业务路由统一挂鉴权依赖（T3.1）：新增路由照此挂载，白名单接口单独处理
app.include_router(collect_router, dependencies=[Depends(require_user)])
app.include_router(dashboard_router, dependencies=[Depends(require_user)])
app.include_router(report_router, dependencies=[Depends(require_user)])
app.include_router(selector_router, dependencies=[Depends(require_user)])
app.include_router(backtest_router, dependencies=[Depends(require_user)])
app.include_router(hot_router, dependencies=[Depends(require_user)])
app.include_router(news_router, dependencies=[Depends(require_user)])
app.include_router(watchlist_router, dependencies=[Depends(require_user)])
app.include_router(task_router, dependencies=[Depends(require_user)])
app.include_router(config_router, dependencies=[Depends(require_user)])
app.include_router(datasource_router, dependencies=[Depends(require_user)])


if settings.api_docs_enabled:

    @app.get("/docs", include_in_schema=False)
    async def docs_alias():
        """开发文档约定 /docs 可浏览（重定向到 /api/docs）；随 API_DOCS_ENABLED 一并关闭。"""
        return RedirectResponse("/api/docs")

# ---- 前端静态托管（镜像内 frontend/dist 存在时，开发文档 §8.1 单镜像交付）----
_DIST = Path(settings.frontend_dist)
if _DIST.exists():
    from fastapi.staticfiles import StaticFiles

    if (_DIST / "assets").exists():
        app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    def _iter_route_methods(routes, prefix: str = "") -> list[tuple[str, set[str]]]:
        """递归展开路由注册表为 (路径, 可用方法) 列表。

        兼容 FastAPI 新版（≥0.16x）：include_router 不再把子路由平铺进 app.routes，
        而是包装成 _IncludedRouter（本身没有 path/methods），真实路由挂在
        original_router.routes 上；旧版则是直接平铺的 APIRoute/Mount。
        """
        out: list[tuple[str, set[str]]] = []
        for route in routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if path is not None and methods is not None:
                out.append((prefix + path, set(methods)))
                continue
            ctx = getattr(route, "include_context", None)
            sub_prefix = (prefix + (getattr(ctx, "prefix", "") or "")
                          if ctx is not None else prefix)
            sub = getattr(getattr(route, "original_router", None), "routes", None)
            if sub is None:
                sub = getattr(route, "routes", None)
            if sub:
                out.extend(_iter_route_methods(sub, sub_prefix))
        return out

    _API_METHODS: dict[str, set[str]] = {}

    def _build_api_methods() -> None:
        """建立 /api 路径 → 可用方法集合的索引（首次调用建立后缓存）。"""
        if _API_METHODS:
            return
        for path, methods in _iter_route_methods(app.routes):
            if path.startswith("/api"):
                _API_METHODS.setdefault(path, set()).update(methods)

    def _api_allowed_methods(request_path: str) -> set[str]:
        """D-09 修复：收集已注册的同路径 API 路由所支持的方法。

        SPA 兜底路由会吞掉所有 GET 请求，导致「路径存在但方法不匹配」被误判为
        404。此处按路由注册表反查真实可用方法，用于正确返回 405。
        """
        _build_api_methods()
        return set(_API_METHODS.get(request_path, set()))

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        # 非 /api 路径一律回前端入口（前端路由自行处理），避免深链接 404
        if full_path.startswith("api/"):
            # D-09：路径已注册但当前方法不支持 → 405（带 Allow 头）；路径不存在 → 404
            allowed = _api_allowed_methods("/" + full_path)
            allowed.discard("GET")
            allowed.discard("HEAD")
            if allowed:
                allow_header = ", ".join(sorted(allowed | {"GET", "OPTIONS"}))
                raise HTTPException(
                    status_code=405,
                    detail=f"方法不允许（{'/' + full_path} 仅支持 {allow_header}）",
                    headers={"Allow": allow_header},
                )
            raise HTTPException(status_code=404, detail="接口不存在")
        return FileResponse(_DIST / "index.html")