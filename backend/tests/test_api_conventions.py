"""T3.1 验收：API 规范收口（分页统一 / 错误码表 / 鉴权统一挂载 / OpenAPI 可浏览）。"""
import pytest
from pydantic import ValidationError

from app.core.errors import ERROR_CODES, AppError
from app.schemas.common import Page, Pagination
from app.api.deps import get_pagination


# ---------- 分页统一 ----------

def test_pagination_defaults_bounds_and_offsets():
    assert Pagination().page == 1 and Pagination().page_size == 20
    p = Pagination(page=3, page_size=50)
    assert p.offset == 100 and p.limit == 50
    for bad in ({"page": 0}, {"page": -1}, {"page_size": 0}, {"page_size": 201}):
        with pytest.raises(ValidationError):
            Pagination(**bad)
    dep = get_pagination(page=5, page_size=10)
    assert dep.page == 5 and dep.page_size == 10  # 查询参数依赖注入

    page = Page(items=[1, 2], total=10, page=1, page_size=2)
    assert page.model_dump() == {"items": [1, 2], "total": 10, "page": 1, "page_size": 2}


# ---------- 错误码表 ----------

def test_error_code_table_covers_in_use_codes():
    # 项目中已使用/约定的错误码都必须在表内登记（新增先登记再使用）
    for code in (400, 401, 403, 404, 409, 429, 500, 502, 503):
        assert code in ERROR_CODES and ERROR_CODES[code]
    err = AppError(502, "全部接口失败")
    assert err.code == 502 and err.message == "全部接口失败"


def test_error_envelope_shape(client):
    # 登录后访问不存在接口 → 404 统一信封（未登录时中间件先返回 401）
    client.post("/api/auth/login",
                json={"username": "admin", "password": "test-pass-123"})
    r = client.get("/api/nonexistent")
    assert r.status_code == 404
    body = r.json()
    assert set(body) == {"code", "message"} and body["code"] == 404


# ---------- 鉴权统一挂载（业务路由 401，白名单放行） ----------

def test_auth_mounted_on_business_routers(client):
    r = client.post("/api/collect/trigger", json={"mode": "incremental"})
    assert r.status_code == 401 and r.json()["code"] == 401  # 业务路由统一鉴权

    assert client.get("/api/health").status_code == 200        # 白名单
    r2 = client.post("/api/auth/login", json={})                # 白名单（校验失败≠401）
    assert r2.status_code == 400 and r2.json()["code"] == 400


# ---------- OpenAPI 可浏览 ----------

def test_docs_browsable_and_openapi_lists_routes(client):
    r = client.get("/docs", follow_redirects=False)
    assert r.status_code in (302, 307) and "/api/docs" in r.headers["location"]
    assert client.get("/api/docs").status_code == 200

    spec = client.get("/api/openapi.json").json()
    paths = spec["paths"]
    for p in ("/api/health", "/api/auth/login", "/api/auth/session",
              "/api/auth/logout", "/api/auth/password", "/api/collect/trigger"):
        assert p in paths, f"OpenAPI 缺接口 {p}"
    assert "统一错误格式" in spec["info"]["description"]  # 约定随文档可浏览
