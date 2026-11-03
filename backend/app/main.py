"""DailyStockAI 后端入口（开发文档 §2.1：单镜像由 FastAPI 托管前端产物）。"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.core.config import get_settings
from app.core.errors import install_error_handlers
from app.core.logging import setup_logging

setup_logging()
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # T0.4 起：init_db() + 管理员种子；T1.1 起换 Alembic 迁移
    yield


app = FastAPI(title=settings.app_name, version="0.1.0",
              docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=lifespan)

install_error_handlers(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- API 路由 ----
app.include_router(health_router)

# ---- 前端静态托管（镜像内 frontend/dist 存在时）----
_DIST = Path(settings.frontend_dist)
if _DIST.exists():
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse

    if (_DIST / "assets").exists():
        app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        # 非 /api 路径一律回前端入口（前端路由自行处理），避免深链接 404
        if full_path.startswith("api/"):
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="接口不存在")
        index = _DIST / "index.html"
        return FileResponse(index if index.exists() else _DIST / "index.html")
