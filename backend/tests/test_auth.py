"""登录鉴权验收（开发文档 §3.4 / T0.4 验收标准）。"""

ADMIN_USER = "admin"
ADMIN_PASS = "test-pass-123"


def test_health_no_auth(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_unauth_api_returns_401(client):
    r = client.get("/api/auth/session")
    assert r.status_code == 401
    assert r.json()["code"] == 401


def test_login_and_bearer_session(client):
    r = client.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert r.status_code == 200
    token = r.json()["token"]
    r2 = client.get("/api/auth/session", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200
    assert r2.json() == {"username": ADMIN_USER}


def test_cookie_session(client):
    r = client.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert r.status_code == 200
    r2 = client.get("/api/auth/session")  # TestClient 自动携带 Set-Cookie
    assert r2.status_code == 200
    assert r2.json()["username"] == ADMIN_USER


def test_wrong_password_401(client):
    r = client.post("/api/auth/login", json={"username": ADMIN_USER, "password": "wrong-pass"})
    assert r.status_code == 401
    assert r.json()["code"] == 401


def test_login_lockout(client):
    # 用独立账号触发限速，避免影响 admin 的其他用例
    for _ in range(5):
        r = client.post("/api/auth/login", json={"username": "attacker", "password": "x"})
        assert r.status_code == 401
    r = client.post("/api/auth/login", json={"username": "attacker", "password": "x"})
    assert r.status_code == 429
    assert "锁定" in r.json()["message"] or "重试" in r.json()["message"]


def test_change_password_invalidates_old_token(client):
    r = client.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
    token = r.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    r2 = client.post(
        "/api/auth/password",
        json={"old_password": ADMIN_PASS, "new_password": "new-pass-456"},
        headers=headers,
    )
    assert r2.status_code == 200

    # 旧 token 应失效（密码指纹变更）
    r3 = client.get("/api/auth/session", headers=headers)
    assert r3.status_code == 401

    # 新密码可登录
    r4 = client.post("/api/auth/login", json={"username": ADMIN_USER, "password": "new-pass-456"})
    assert r4.status_code == 200
