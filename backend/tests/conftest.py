"""pytest 环境：SQLite 临时库 + 固定初始密码（在导入 app 前设置）。"""
import os
import tempfile

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/test.db"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ADMIN_INITIAL_PASSWORD"] = "test-pass-123"
# T7.5：TestClient 会触发 lifespan，须在导入 app 之前关闭进程内调度器，
# 否则测试期间会真起后台调度线程（调度链路由 tests/test_scheduler.py 单独验证）。
os.environ["SCHEDULER_ENABLED"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c