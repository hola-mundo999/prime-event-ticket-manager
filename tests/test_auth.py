import os
import tempfile
import time

import auth
import main
import models
import pytest
from fastapi.testclient import TestClient
from main import app
from test_support import authorize_admin, csrf_headers, make_totp

client = TestClient(app)
PASSWORD = "prime-desk-2026"


def setup_function(function):
    auth._FAILURES.clear()
    client.cookies.clear()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    models.DB_PATH = path
    main.store = models.DataStore(db_path=path)
    main.store.create_user(auth.ADMIN_EMAIL, "Samyog Sapkota", PASSWORD, role="owner")


def teardown_function(function):
    auth._FAILURES.clear()
    try:
        os.remove(models.DB_PATH)
    except OSError:
        pass


def password_login(email=auth.ADMIN_EMAIL, password=PASSWORD):
    return client.post("/api/admin/login", json={"email": email, "password": password})


def enroll():
    assert password_login().status_code == 200
    return client.post("/api/admin/mfa/enroll/start")


def test_password_only_and_pending_enrollment_cannot_access_admin_api():
    response = password_login()
    assert response.status_code == 200
    assert response.json()["mfa_required"] is True
    assert response.json()["enrollment_required"] is True
    assert "token" not in response.json()
    assert client.get("/api/admin/session").status_code == 401

    enrollment = client.post("/api/admin/mfa/enroll/start")
    assert enrollment.status_code == 200
    assert client.get("/api/admin/overview").status_code == 401


def test_enrollment_requires_a_valid_totp_and_encrypts_the_secret():
    setup = enroll()
    assert setup.status_code == 200
    body = setup.json()
    secret = body["secret"]
    assert len(secret) == 32
    assert body["provisioning_uri"].startswith("otpauth://totp/")
    assert "algorithm=SHA1&digits=6&period=30" in body["provisioning_uri"]
    assert body["qr_code"].startswith("data:image/png;base64,")

    user = main.store.find_user(auth.ADMIN_EMAIL)
    assert user["pending_totp_secret"] != secret
    assert auth.decrypt_totp_secret(user["pending_totp_secret"]) == secret

    invalid = client.post("/api/admin/mfa/enroll/activate", json={"code": "000000"})
    assert invalid.status_code == 401
    assert client.get("/api/admin/session").status_code == 401

    valid = client.post(
        "/api/admin/mfa/enroll/activate", json={"code": make_totp(secret)}
    )
    assert valid.status_code == 200, valid.text
    assert len(valid.json()["recovery_codes"]) == 10
    assert client.get("/api/admin/session").status_code == 200


def test_totp_login_rejects_bad_codes_and_accepts_a_valid_code():
    first = authorize_admin(client, password=PASSWORD)
    assert first.status_code == 200
    client.cookies.clear()

    primary = password_login()
    assert primary.status_code == 200
    invalid = client.post("/api/admin/mfa/verify", json={"code": "123456"})
    assert invalid.status_code == 401
    assert client.get("/api/admin/session").status_code == 401

    primary = password_login()
    assert primary.status_code == 200
    stored = main.store.find_user(auth.ADMIN_EMAIL)
    secret = auth.decrypt_totp_secret(stored["totp_secret"])
    valid = client.post(
        "/api/admin/mfa/verify", json={"code": make_totp(secret, time.time() + 30)}
    )
    assert valid.status_code == 200, valid.text
    assert client.get("/api/admin/session").status_code == 200


def test_totp_code_cannot_be_replayed_for_another_session():
    assert authorize_admin(client, password=PASSWORD).status_code == 200
    client.cookies.clear()
    stored = main.store.find_user(auth.ADMIN_EMAIL)
    secret = auth.decrypt_totp_secret(stored["totp_secret"])
    code = make_totp(secret, time.time() + 30)
    assert password_login().status_code == 200
    assert client.post("/api/admin/mfa/verify", json={"code": code}).status_code == 200
    client.post("/api/admin/logout", headers=csrf_headers(client))
    assert password_login().status_code == 200
    assert client.post("/api/admin/mfa/verify", json={"code": code}).status_code == 401


def test_recovery_code_is_hashed_single_use_and_not_returned_again():
    setup = enroll()
    secret = setup.json()["secret"]
    activation = client.post(
        "/api/admin/mfa/enroll/activate", json={"code": make_totp(secret)}
    )
    assert activation.status_code == 200
    recovery = activation.json()["recovery_codes"][0]
    client.cookies.clear()

    primary = password_login()
    assert primary.status_code == 200
    failed = client.post("/api/admin/mfa/verify", json={"code": "not-a-valid-recovery"})
    assert failed.status_code == 401

    primary = password_login()
    assert primary.status_code == 200
    success = client.post("/api/admin/mfa/verify", json={"code": recovery})
    assert success.status_code == 200, success.text
    assert "recovery_codes" not in success.json()
    client.post("/api/admin/logout", headers=csrf_headers(client))

    assert password_login().status_code == 200
    reused = client.post("/api/admin/mfa/verify", json={"code": recovery})
    assert reused.status_code == 401
    conn = main.store._connect()
    try:
        rows = conn.execute(
            "SELECT code_hash FROM admin_recovery_codes WHERE user_id = 1"
        ).fetchall()
    finally:
        conn.close()
    assert auth.recovery_code_hash(recovery) not in [row["code_hash"] for row in rows]
    assert recovery not in [row["code_hash"] for row in rows]


def test_only_allowlisted_email_can_be_created_or_get_a_session():
    with pytest.raises(ValueError):
        main.store.create_user("other@example.com", "Other", PASSWORD, role="owner")
    conn = main.store._connect()
    try:
        conn.execute(
            "INSERT INTO admin_users "
            "(email,name,password_hash,role,active,created_at,mfa_enabled,totp_secret) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "other@example.com",
                "Other",
                auth.hash_password(PASSWORD),
                "owner",
                True,
                "2026-10-31T00:00:00+00:00",
                True,
                auth.encrypt_totp_secret(auth.new_totp_secret()),
            ),
        )
        conn.commit()
        other = conn.execute(
            "SELECT id FROM admin_users WHERE email = ?", ("other@example.com",)
        ).fetchone()
    finally:
        conn.close()
    legacy = main.store.start_session(int(other["id"]))
    assert main.store.session_user(legacy["token"]) is None
    response = password_login("other@example.com", PASSWORD)
    assert response.status_code == 401
    assert client.get("/api/admin/session").status_code == 401


def test_bootstrap_creates_allowlisted_admin_alongside_legacy_users(monkeypatch, tmp_path):
    database = str(tmp_path / "legacy-users.db")
    main.store = models.DataStore(db_path=database)
    conn = main.store._connect()
    try:
        conn.execute(
            "INSERT INTO admin_users "
            "(email,name,password_hash,role,active,created_at) VALUES (?,?,?,?,?,?)",
            (
                "former-admin@example.com",
                "Former Admin",
                auth.hash_password(PASSWORD),
                "owner",
                True,
                "2026-10-31T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("ADMIN_EMAIL", auth.ADMIN_EMAIL)
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    message = main.bootstrap_admin()
    assert message and message.startswith("Created")
    assert main.store.find_user(auth.ADMIN_EMAIL)
    assert main.store.find_user("former-admin@example.com")


def test_account_provisioning_and_management_are_disabled():
    assert authorize_admin(client, password=PASSWORD).status_code == 200
    headers = csrf_headers(client)
    created = client.post(
        "/api/admin/users",
        headers=headers,
        json={
            "email": "other@example.com",
            "name": "Other",
            "password": "another-pass-2026",
            "role": "owner",
        },
    )
    assert created.status_code == 403
    assert main.store.user_count() == 1
    changed = client.patch(
        "/api/admin/users/1", headers=headers, json={"role": "staff"}
    )
    assert changed.status_code == 403
    assert main.store.find_user(auth.ADMIN_EMAIL)["role"] == "owner"


def test_legacy_sessions_are_rejected_after_mfa_migration():
    legacy = main.store.start_session(1)
    assert main.store.session_user(legacy["token"]) is None

    conn = main.store._connect()
    try:
        conn.execute("DELETE FROM admin_security_migrations")
        conn.commit()
        conn.execute(
            "INSERT INTO admin_sessions "
            "(token_hash,user_id,created_at,expires_at,last_seen_at,user_agent,csrf_hash) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                auth.token_fingerprint(legacy["token"]),
                1,
                "2026-10-31T00:00:00+00:00",
                "2026-11-01T00:00:00+00:00",
                "2026-10-31T00:00:00+00:00",
                "",
                None,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    models.DataStore(db_path=models.DB_PATH)
    assert main.store.session_user(legacy["token"]) is None


def test_mfa_failure_attempts_are_rate_limited():
    assert authorize_admin(client, password=PASSWORD).status_code == 200
    client.cookies.clear()
    assert password_login().status_code == 200
    for _ in range(auth.MAX_MFA_FAILURES):
        assert client.post("/api/admin/mfa/verify", json={"code": "000000"}).status_code == 401
    limited = client.post("/api/admin/mfa/verify", json={"code": "000000"})
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0


def test_password_attempts_are_rate_limited():
    for _ in range(auth.MAX_FAILURES):
        assert password_login(password="incorrect-password-1").status_code == 401
    limited = password_login(password="incorrect-password-1")
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0


def test_password_rate_limit_is_shared_between_api_clients():
    for _ in range(auth.MAX_FAILURES):
        assert password_login(password="incorrect-password-1").status_code == 401
    auth._FAILURES.clear()
    other_client = TestClient(app)
    limited = other_client.post(
        "/api/admin/login",
        json={"email": auth.ADMIN_EMAIL, "password": PASSWORD},
    )
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0


def test_recovery_code_attempts_are_rate_limited():
    assert authorize_admin(client, password=PASSWORD).status_code == 200
    client.cookies.clear()
    assert password_login().status_code == 200
    for _ in range(auth.MAX_MFA_FAILURES):
        assert client.post(
            "/api/admin/mfa/verify", json={"code": "invalid-recovery-code"}
        ).status_code == 401
    limited = client.post(
        "/api/admin/mfa/verify", json={"code": "invalid-recovery-code"}
    )
    assert limited.status_code == 429


def test_session_and_database_never_return_totp_or_recovery_secrets():
    setup = enroll()
    secret = setup.json()["secret"]
    activation = client.post(
        "/api/admin/mfa/enroll/activate", json={"code": make_totp(secret)}
    )
    recovery_codes = activation.json()["recovery_codes"]
    session = client.get("/api/admin/session")
    assert session.status_code == 200
    assert "totp_secret" not in session.text
    assert "recovery_codes" not in session.text
    assert secret not in session.text
    assert all(code not in session.text for code in recovery_codes)
    assert "token" not in activation.json()
    assert client.post("/api/admin/mfa/enroll/start").status_code == 401

    conn = main.store._connect()
    try:
        user = conn.execute(
            "SELECT totp_secret, pending_totp_secret FROM admin_users WHERE id = 1"
        ).fetchone()
        hashes = conn.execute(
            "SELECT code_hash FROM admin_recovery_codes WHERE user_id = 1"
        ).fetchall()
    finally:
        conn.close()
    assert user["pending_totp_secret"] is None
    assert secret not in user["totp_secret"]
    assert all(code not in [row["code_hash"] for row in hashes] for code in recovery_codes)


def test_session_cookie_is_http_only_and_mutations_require_csrf():
    response = authorize_admin(client, password=PASSWORD)
    assert response.status_code == 200
    cookie = client.cookies.get("prime_admin_session")
    assert cookie
    set_cookie = response.headers.get("set-cookie", "").lower()
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    bad = client.patch("/api/admin/profile", json={"name": "No CSRF"})
    assert bad.status_code == 403
    assert client.patch(
        "/api/admin/profile",
        headers=csrf_headers(client),
        json={"name": "Updated Name"},
    ).status_code == 200


def test_missing_encryption_key_fails_closed(monkeypatch):
    assert authorize_admin(client, password=PASSWORD).status_code == 200
    client.cookies.clear()
    assert password_login().status_code == 200
    monkeypatch.delenv("ADMIN_TOTP_ENCRYPTION_KEY")
    response = client.post("/api/admin/mfa/verify", json={"code": "123456"})
    assert response.status_code == 503
