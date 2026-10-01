import pytest
import time
import datetime
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.auth import create_access_token, LOGIN_ATTEMPTS, clear_login_failures, ENTERPRISE_USERS

client = TestClient(app)

def get_auth_headers(email: str, password: str = "password123"):
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, f"Login failed for {email}: {res.text}"
    token = res.json()["token"]
    return {"Authorization": f"Bearer {token}"}

# =====================================================
# PHASE A1: TOKEN EXPIRY & REFRESH ROTATION TESTS
# =====================================================

def test_refresh_token_rotation_and_replay_prevention():
    # 1. Login and get refresh token
    res = client.post("/api/auth/login", json={
        "email": "sarah.connor@support-ai.enterprise",
        "password": "password123"
    })
    assert res.status_code == 200
    data = res.json()
    assert "token" in data
    assert "refresh_token" in data
    old_rt = data["refresh_token"]

    # 2. Rotate refresh token
    ref_res = client.post("/api/auth/refresh", json={"refresh_token": old_rt})
    assert ref_res.status_code == 200
    ref_data = ref_res.json()
    new_token = ref_data["token"]
    new_rt = ref_data["refresh_token"]
    assert new_rt != old_rt

    # 3. New access token can access protected endpoints
    me_res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {new_token}"})
    assert me_res.status_code == 200
    assert me_res.json()["user"]["email"] == "sarah.connor@support-ai.enterprise"

    # 4. Attempting to replay old refresh token -> 401
    replay_res = client.post("/api/auth/refresh", json={"refresh_token": old_rt})
    assert replay_res.status_code == 401

def test_logout_revocation():
    # 1. Login
    res = client.post("/api/auth/login", json={
        "email": "marcus.wright@support-ai.enterprise",
        "password": "password123"
    })
    assert res.status_code == 200
    data = res.json()
    token = data["token"]
    rt = data["refresh_token"]

    # 2. Logout with refresh token
    logout_res = client.post(
        "/api/auth/logout",
        json={"refresh_token": rt},
        headers={"Authorization": f"Bearer {token}"}
    )
    assert logout_res.status_code == 200

    # 3. Using revoked refresh token fails
    ref_res = client.post("/api/auth/refresh", json={"refresh_token": rt})
    assert ref_res.status_code == 401

def test_token_expiry_handling():
    # Manually issue an access token that is already expired
    user = ENTERPRISE_USERS["sarah.connor@support-ai.enterprise"]
    expired_token, _ = create_access_token(user, custom_expire_minutes=-5)

    res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired_token}"})
    assert res.status_code == 401
    assert "expired" in res.json()["detail"].lower()

# =====================================================
# PHASE A1: RATE LIMITING LOCKOUT
# =====================================================

def test_login_brute_force_lockout():
    test_email = "lockout.test@support-ai.enterprise"
    clear_login_failures(test_email, "testclient")

    # Send 5 failed attempts
    for _ in range(5):
        fail_res = client.post("/api/auth/login", json={
            "email": test_email,
            "password": "wrong_password"
        })
        assert fail_res.status_code == 401

    # 6th attempt triggers 429 Too Many Requests
    lock_res = client.post("/api/auth/login", json={
        "email": test_email,
        "password": "wrong_password"
    })
    assert lock_res.status_code == 429
    assert "too many" in lock_res.json()["detail"].lower()

    # Clean up for other tests
    clear_login_failures(test_email, "testclient")

# =====================================================
# PHASE A1: ADMIN USER MANAGEMENT & RBAC
# =====================================================

def test_admin_user_management_crud():
    admin_headers = get_auth_headers("sarah.connor@support-ai.enterprise")

    # 1. List users
    list_res = client.get("/api/users", headers=admin_headers)
    assert list_res.status_code == 200
    users = list_res.json()
    assert len(users) >= 5

    # 2. Create new user
    new_email = f"john.doe.{int(time.time())}@enterprise.local"
    create_res = client.post("/api/users", json={
        "name": "John Doe",
        "email": new_email,
        "password": "Password123!",
        "role": "SUPPORT_AGENT",
        "organization": "Enterprise Global Technologies"
    }, headers=admin_headers)
    assert create_res.status_code == 200
    new_user = create_res.json()
    user_id = new_user["id"]
    assert new_user["email"] == new_email
    assert new_user["role"] == "SUPPORT_AGENT"

    # 3. Change role
    role_res = client.patch(f"/api/users/{user_id}/role", json={"role": "SUPPORT_MANAGER"}, headers=admin_headers)
    assert role_res.status_code == 200
    assert role_res.json()["role"] == "SUPPORT_MANAGER"

    # 4. Deactivate user
    status_res = client.patch(f"/api/users/{user_id}/status", json={"is_active": False}, headers=admin_headers)
    assert status_res.status_code == 200
    assert status_res.json()["is_active"] is False

    # 5. Deactivated user cannot login
    login_res = client.post("/api/auth/login", json={"email": new_email, "password": "Password123!"})
    # Since account is deactivated, it either fails auth or me check
    if login_res.status_code == 200:
        me_check = client.get("/api/auth/me", headers={"Authorization": f"Bearer {login_res.json()['token']}"})
        assert me_check.status_code == 401

def test_non_admin_forbidden_from_user_management():
    # SUPPORT_AGENT tries to access /api/users
    agent_headers = get_auth_headers("elena.rostova@support-ai.enterprise")
    res = client.get("/api/users", headers=agent_headers)
    assert res.status_code == 403

    # VIEWER tries to access /api/users
    viewer_headers = get_auth_headers("rachel.green@support-ai.enterprise")
    res_v = client.get("/api/users", headers=viewer_headers)
    assert res_v.status_code == 403

def test_all_5_roles_permission_matrix():
    roles_users = [
        ("sarah.connor@support-ai.enterprise", "ADMIN"),
        ("marcus.wright@support-ai.enterprise", "SUPPORT_MANAGER"),
        ("elena.rostova@support-ai.enterprise", "SUPPORT_AGENT"),
        ("david.chen@support-ai.enterprise", "AUDITOR"),
        ("rachel.green@support-ai.enterprise", "VIEWER")
    ]

    for email, role in roles_users:
        headers = get_auth_headers(email)
        me_res = client.get("/api/auth/me", headers=headers)
        assert me_res.status_code == 200
        user = me_res.json()["user"]
        assert user["role"] == role

        # Admin only
        users_res = client.get("/api/users", headers=headers)
        if role == "ADMIN":
            assert users_res.status_code == 200
        else:
            assert users_res.status_code == 403

        # Tools manager (Admin only)
        tools_res = client.get("/api/tools", headers=headers)
        if role == "ADMIN":
            assert tools_res.status_code == 200
        else:
            assert tools_res.status_code == 403

        # Audit logs (Admin, Support Manager, Auditor)
        audit_res = client.get("/api/audit-logs", headers=headers)
        if role in ["ADMIN", "SUPPORT_MANAGER", "AUDITOR"]:
            assert audit_res.status_code == 200
        else:
            assert audit_res.status_code == 403
