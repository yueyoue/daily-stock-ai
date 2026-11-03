"""API 公共依赖（T3.1 规范收口）。

- **鉴权统一挂载**：业务路由在 main.py `include_router(..., dependencies=[Depends(require_user)])`
  统一挂本依赖（同一依赖每请求只执行一次）；白名单接口（health/login/docs）不挂
- **分页统一**：列表接口用 `Depends(get_pagination)` 取 page/page_size（开发文档 §7）
"""
from fastapi import Query

from app.core.auth import require_user  # noqa: F401  路由统一挂载入口
from app.schemas.common import Page, Pagination  # noqa: F401  统一 DTO 出口


def get_pagination(
    page: int = Query(1, ge=1, description="页码，从 1 起"),
    page_size: int = Query(20, ge=1, le=200, description="每页条数（1~200）"),
) -> Pagination:
    """分页参数依赖：非法值由统一校验错误格式返回 400。"""
    return Pagination(page=page, page_size=page_size)
