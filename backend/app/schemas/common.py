"""通用 DTO（T3.1 API 规范收口，开发文档 §7 约定）。

- 列表接口统一分页参数：page（页码从 1 起）/ page_size（1~200）
- 列表响应统一出参：{items, total, page, page_size}
"""
from pydantic import BaseModel, Field


class Pagination(BaseModel):
    """分页入参（配合 api.deps.get_pagination 作为查询参数依赖）。"""

    page: int = Field(1, ge=1, description="页码，从 1 起")
    page_size: int = Field(20, ge=1, le=200, description="每页条数（1~200）")

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


class Page(BaseModel):
    """列表响应统一出参。"""

    items: list
    total: int
    page: int
    page_size: int
