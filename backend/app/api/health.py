"""健康检查（开发文档 §7：Docker 健康检查 / 反代探活，免鉴权）。"""
from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def health():
    return {"status": "ok"}
