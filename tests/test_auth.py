import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

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


def concurrent_mfa_requests(path, challenge, code, count=12):
    barrier = threading.Barrier(count)
    challenges = challenge if isinstance(challenge, list) else [challenge]

    def send_request(index):
        with TestClient(
            app, client=(f"198.51.100.{index + 1}", 12345)
        ) as concurrent_client:
            concurrent_client.cookies.set(
                "prime_admin_challenge",
                challenges[index % len(challenges)],
                path="/api/admin/mfa",
            )
            barrier.wait(timeout=10)
            response = concurrent_client.post(
                path,
                json={"code": code},
            )
            return response.status_code

    with ThreadPoolExecutor(max_workers=count) as executor:
        return list(executor.map(send_request, range(count)))


def enable_mfa():
    setup = enroll()
    secret = setup.json()["secret"]
    activation = client.post(
        "/api/admin/mfa/enroll/activate", json={"code": make_totp(secret)}
    )
    assert activation.status_code == 200
    assert client.post("/api/admin/logout", headers=csrf_headers(client)).status_code == 200
    client.cookies.clear()
    return secret


def issue_mfa_challenge(source_ip):
    with TestClient(app, client=(source_ip, 12345)) as source_client:
        response = source_client.post(
            "/api/admin/login",
            json={"email": auth.ADMIN_EMAIL, "password": PASSWORD},
        )
        assert response.status_code == 200, response.text
        assert response.json()["mfa_required"] is True
        return source_client.cookies.get("prime_admin_challenge")


def submit_mfa_code(source_ip, challenge, code, path="/api/admin/mfa/verify"):
    with TestClient(app, client=(source_ip, 12345)) as source_client:
        source_client.cookies.set(
            "prime_admin_challenge", challenge, path="/api/admin/mfa"
        )
        return source_client.post(path, json={"code": code})


def account_mfa_failure_state():
    user = main.store.find_user(auth.ADMIN_EMAIL)
    return user["mfa_failure_attempts"], user["mfa_failure_window_started_at"]


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


def test_existing_database_migration_adds_account_mfa_failure_state():
    conn = main.store._connect()
    try:
        conn.execute("ALTER TABLE admin_users DROP COLUMN mfa_failure_attempts")
        conn.execute(
            "ALTER TABLE admin_users DROP COLUMN mfa_failure_window_started_at"
        )
        conn.commit()
    finally:
        conn.close()

    main.store = models.DataStore(db_path=models.DB_PATH)
    user = main.store.find_user(auth.ADMIN_EMAIL)

    assert user["mfa_failure_attempts"] == 0
    assert user["mfa_failure_window_started_at"] is None


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


def test_concurrent_enrollment_activation_creates_only_one_recovery_batch():
    setup = enroll()
    challenges = [client.cookies.get("prime_admin_challenge")]
    client.cookies.clear()
    assert password_login().status_code == 200
    challenges.append(client.cookies.get("prime_admin_challenge"))
    statuses = concurrent_mfa_requests(
        "/api/admin/mfa/enroll/activate",
        challenges,
        make_totp(setup.json()["secret"]),
    )

    assert statuses.count(200) == 1
    assert all(status in (200, 401) for status in statuses)
    conn = main.store._connect()
    try:
        enabled = conn.execute(
            "SELECT mfa_enabled FROM admin_users WHERE id = 1"
        ).fetchone()["mfa_enabled"]
        recovery_count = conn.execute(
            "SELECT COUNT(*) AS c FROM admin_recovery_codes WHERE user_id = 1"
        ).fetchone()["c"]
        session_count = conn.execute(
            "SELECT COUNT(*) AS c FROM admin_sessions WHERE user_id = 1"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert enabled
    assert recovery_count == 10
    assert session_count == 1


def test_concurrent_mfa_failures_cannot_exceed_challenge_attempt_limit():
    setup = enroll()
    activation = client.post(
        "/api/admin/mfa/enroll/activate",
        json={"code": make_totp(setup.json()["secret"])},
    )
    assert activation.status_code == 200
    assert client.post("/api/admin/logout", headers=csrf_headers(client)).status_code == 200

    assert password_login().status_code == 200
    challenge = client.cookies.get("prime_admin_challenge")
    statuses = concurrent_mfa_requests(
        "/api/admin/mfa/verify", challenge, "000000"
    )

    assert 200 not in statuses
    assert all(status in (401, 429) for status in statuses)
    conn = main.store._connect()
    try:
        attempts = conn.execute(
            "SELECT attempts FROM admin_auth_challenges WHERE challenge_hash = ?",
            (auth.token_fingerprint(challenge),),
        ).fetchone()["attempts"]
    finally:
        conn.close()
    assert attempts == auth.MAX_MFA_FAILURES


def test_concurrent_totp_verification_creates_only_one_session():
    setup = enroll()
    activation = client.post(
        "/api/admin/mfa/enroll/activate",
        json={"code": make_totp(setup.json()["secret"])},
    )
    assert activation.status_code == 200
    assert client.post("/api/admin/logout", headers=csrf_headers(client)).status_code == 200

    assert password_login().status_code == 200
    challenges = [client.cookies.get("prime_admin_challenge")]
    client.cookies.clear()
    assert password_login().status_code == 200
    challenges.append(client.cookies.get("prime_admin_challenge"))
    stored = main.store.find_user(auth.ADMIN_EMAIL)
    secret = auth.decrypt_totp_secret(stored["totp_secret"])
    code = make_totp(secret, time.time() + 30)
    statuses = concurrent_mfa_requests("/api/admin/mfa/verify", challenges, code)

    assert statuses.count(200) == 1
    assert all(status in (200, 401, 429) for status in statuses)
    conn = main.store._connect()
    try:
        session_count = conn.execute(
            "SELECT COUNT(*) AS c FROM admin_sessions WHERE user_id = 1"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert session_count == 1


def test_fresh_challenges_do_not_reset_account_mfa_cooldown():
    enable_mfa()
    source_ip = "198.51.100.55"
    for _ in range(auth.MAX_MFA_FAILURES):
        challenge = issue_mfa_challenge(source_ip)
        assert submit_mfa_code(source_ip, challenge, "000000").status_code == 401

    auth.clear_failures(source_ip)
    main.store.clear_auth_failures(source_ip)
    new_challenge = issue_mfa_challenge(source_ip)
    limited = submit_mfa_code(source_ip, new_challenge, "000000")

    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0
    assert account_mfa_failure_state()[0] == auth.MAX_MFA_FAILURES


def test_mfa_failures_from_different_sources_share_account_limit():
    enable_mfa()
    for index in range(auth.MAX_MFA_FAILURES):
        source_ip = f"198.51.100.{100 + index}"
        challenge = issue_mfa_challenge(source_ip)
        assert submit_mfa_code(source_ip, challenge, "000000").status_code == 401

    source_ip = "198.51.100.200"
    challenge = issue_mfa_challenge(source_ip)
    limited = submit_mfa_code(source_ip, challenge, "000000")

    assert limited.status_code == 429
    assert account_mfa_failure_state()[0] == auth.MAX_MFA_FAILURES


def test_successful_mfa_authentication_resets_account_failure_state():
    secret = enable_mfa()
    for index in range(2):
        source_ip = f"198.51.100.{210 + index}"
        challenge = issue_mfa_challenge(source_ip)
        assert submit_mfa_code(source_ip, challenge, "000000").status_code == 401

    source_ip = "198.51.100.220"
    challenge = issue_mfa_challenge(source_ip)
    response = submit_mfa_code(
        source_ip, challenge, make_totp(secret, time.time() + 30)
    )

    assert response.status_code == 200, response.text
    assert account_mfa_failure_state() == (0, None)


def test_recovery_code_failures_count_toward_account_mfa_limit():
    enable_mfa()
    for index in range(auth.MAX_MFA_FAILURES):
        source_ip = f"198.51.100.{230 + index}"
        challenge = issue_mfa_challenge(source_ip)
        response = submit_mfa_code(source_ip, challenge, "invalid-recovery-code")
        assert response.status_code == 401

    assert account_mfa_failure_state()[0] == auth.MAX_MFA_FAILURES
    source_ip = "198.51.100.240"
    challenge = issue_mfa_challenge(source_ip)
    assert (
        submit_mfa_code(source_ip, challenge, "invalid-recovery-code").status_code
        == 429
    )


def test_concurrent_mfa_failures_respect_account_limit_across_challenges():
    enable_mfa()
    challenges = [
        issue_mfa_challenge(f"198.51.100.{index + 1}")
        for index in range(12)
    ]
    statuses = concurrent_mfa_requests(
        "/api/admin/mfa/verify", challenges, "000000"
    )

    assert statuses.count(401) == auth.MAX_MFA_FAILURES
    assert statuses.count(429) == len(challenges) - auth.MAX_MFA_FAILURES
    assert account_mfa_failure_state()[0] == auth.MAX_MFA_FAILURES


def test_account_mfa_cooldown_expires_and_starts_a_new_window(monkeypatch):
    enable_mfa()
    monkeypatch.setattr(auth, "MFA_FAILURE_WINDOW_SECONDS", 1)
    for index in range(auth.MAX_MFA_FAILURES):
        source_ip = f"198.51.100.{30 + index}"
        challenge = issue_mfa_challenge(source_ip)
        assert submit_mfa_code(source_ip, challenge, "000000").status_code == 401

    time.sleep(1.1)
    source_ip = "198.51.100.40"
    challenge = issue_mfa_challenge(source_ip)
    assert submit_mfa_code(source_ip, challenge, "000000").status_code == 401
    attempts, window_started_at = account_mfa_failure_state()
    assert attempts == 1
    assert window_started_at is not None


def test_invalid_or_expired_challenges_do_not_increment_account_failures():
    enable_mfa()
    source_ip = "198.51.100.50"
    expired_challenge = issue_mfa_challenge(source_ip)
    conn = main.store._connect()
    try:
        conn.execute(
            "UPDATE admin_auth_challenges SET expires_at = ? WHERE challenge_hash = ?",
            ("2000-01-01T00:00:00+00:00", auth.token_fingerprint(expired_challenge)),
        )
        conn.commit()
    finally:
        conn.close()

    assert (
        submit_mfa_code(source_ip, expired_challenge, "000000").status_code == 401
    )
    assert account_mfa_failure_state() == (0, None)
    assert (
        submit_mfa_code(source_ip, "not-a-valid-challenge", "000000").status_code
        == 401
    )
    assert account_mfa_failure_state() == (0, None)


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


def test_password_rate_limit_is_shared_between_clients_at_same_source():
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


def test_failed_passwords_from_another_source_do_not_lock_out_admin():
    attacker = TestClient(app, client=("198.51.100.10", 12345))
    admin = TestClient(app, client=("198.51.100.20", 12345))
    for _ in range(auth.MAX_FAILURES):
        response = attacker.post(
            "/api/admin/login",
            headers={"X-Forwarded-For": "203.0.113.99"},
            json={"email": auth.ADMIN_EMAIL, "password": "wrong-password-1"},
        )
        assert response.status_code == 401
    blocked = attacker.post(
        "/api/admin/login",
        headers={"X-Forwarded-For": "203.0.113.100"},
        json={"email": auth.ADMIN_EMAIL, "password": "wrong-password-1"},
    )
    assert blocked.status_code == 429

    login = admin.post(
        "/api/admin/login",
        json={"email": auth.ADMIN_EMAIL, "password": PASSWORD},
    )
    assert login.status_code == 200
    assert login.json()["mfa_required"] is True
    assert login.json()["enrollment_required"] is True

    enrollment = admin.post("/api/admin/mfa/enroll/start")
    assert enrollment.status_code == 200
    secret = enrollment.json()["secret"]
    activation = admin.post(
        "/api/admin/mfa/enroll/activate",
        json={"code": make_totp(secret)},
    )
    assert activation.status_code == 200
    admin.post("/api/admin/logout", headers=csrf_headers(admin))

    login = admin.post(
        "/api/admin/login",
        json={"email": auth.ADMIN_EMAIL, "password": PASSWORD},
    )
    assert login.status_code == 200
    stored = main.store.find_user(auth.ADMIN_EMAIL)
    active_secret = auth.decrypt_totp_secret(stored["totp_secret"])
    verified = admin.post(
        "/api/admin/mfa/verify",
        json={"code": make_totp(active_secret, time.time() + 30)},
    )
    assert verified.status_code == 200
    assert admin.get("/api/admin/session").status_code == 200


def test_persistent_auth_failure_cleanup_is_bounded():
    expired_keys = [
        main.store._rate_limit_key_hash(f"expired-source-{index}")
        for index in range(auth.AUTH_FAILURE_CLEANUP_BATCH + 7)
    ]
    active_key = main.store._rate_limit_key_hash("active-source")
    conn = main.store._connect()
    try:
        for key in expired_keys:
            conn.execute(
                "INSERT INTO admin_auth_failures (email_hash, window_started_at, attempts) "
                "VALUES (?, ?, ?)",
                (key, "2000-01-01T00:00:00+00:00", auth.MAX_FAILURES),
            )
        conn.execute(
            "INSERT INTO admin_auth_failures (email_hash, window_started_at, attempts) "
            "VALUES (?, ?, ?)",
            (active_key, auth._iso(auth._utc_now()), 1),
        )
        conn.commit()
    finally:
        conn.close()

    main.store.auth_throttle_remaining("192.0.2.30")
    cutoff = auth._iso(
        auth._utc_now() - timedelta(seconds=auth.FAILURE_WINDOW_SECONDS)
    )
    conn = main.store._connect()
    try:
        expired = conn.execute(
            "SELECT COUNT(*) AS c FROM admin_auth_failures "
            "WHERE window_started_at <= ?",
            (cutoff,),
        ).fetchone()["c"]
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM admin_auth_failures WHERE email_hash = ?",
            (active_key,),
        ).fetchone()["c"]
    finally:
        conn.close()
    assert expired == 7
    assert active == 1

    main.store.auth_throttle_remaining("192.0.2.30")
    conn = main.store._connect()
    try:
        expired = conn.execute(
            "SELECT COUNT(*) AS c FROM admin_auth_failures "
            "WHERE window_started_at <= ?",
            (cutoff,),
        ).fetchone()["c"]
    finally:
        conn.close()
    assert expired == 0


def test_persistent_auth_failure_tracking_has_a_hard_row_cap(monkeypatch):
    monkeypatch.setattr(auth, "MAX_TRACKED_AUTH_FAILURES", 8)
    for index in range(25):
        main.store.note_auth_failure(f"192.0.2.{index + 1}")

    conn = main.store._connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM admin_auth_failures"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert count <= auth.MAX_TRACKED_AUTH_FAILURES


def test_existing_over_capacity_rows_are_trimmed_in_bounded_batches(monkeypatch):
    monkeypatch.setattr(auth, "MAX_TRACKED_AUTH_FAILURES", 8)
    monkeypatch.setattr(auth, "AUTH_FAILURE_CLEANUP_BATCH", 3)
    conn = main.store._connect()
    try:
        for index in range(12):
            conn.execute(
                "INSERT INTO admin_auth_failures (email_hash, window_started_at, attempts) "
                "VALUES (?, ?, ?)",
                (
                    main.store._rate_limit_key_hash(f"legacy-source-{index}"),
                    auth._iso(auth._utc_now()),
                    auth.MAX_FAILURES,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    main.store.auth_throttle_remaining("192.0.2.40")
    conn = main.store._connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM admin_auth_failures"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert count == 9

    main.store.auth_throttle_remaining("192.0.2.40")
    conn = main.store._connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM admin_auth_failures"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert count == auth.MAX_TRACKED_AUTH_FAILURES


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
