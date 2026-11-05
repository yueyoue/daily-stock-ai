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
        "① 鉴权覆盖全部 /api/*（白名单仅 health/login/docs）；"
        "② 统一错误格式 {\"code\": int, \"message\": str}，错误码表见 core/errors.ERROR_CODES；"
        "③ 列表接口统一分页参数 page/page_size，响应 {items,total,page,page_size}。"
    ),
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
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


@app.get("/docs", include_in_schema=False)
async def docs_alias():
    """开发文档约定 /docs 可浏览（重定向到 /api/docs）。"""
    return RedirectResponse("/api/docs")

# ---- 前端静态托管（镜像内 frontend/dist 存在时，开发文档 §8.1 单镜像交付）----
_DIST = Path(settings.frontend_dist)
if _DIST.exists():
    from fastapi.staticfiles import StaticFiles

    if (_DIST / "assets").exists():
        app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        # 非 /api 路径一律回前端入口（前端路由自行处理），避免深链接 404
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="接口不存在")
        return FileResponse(_DIST / "index.html")