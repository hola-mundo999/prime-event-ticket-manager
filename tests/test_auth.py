"""Admin accounts: sign-in, roles, sessions and password rules."""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import auth
import main
import models
from main import app

client = TestClient(app)

OWNER_EMAIL = "owner@primeitclub.com"
OWNER_PASSWORD = "prime-desk-2026"
STAFF_EMAIL = "gate@primeitclub.com"
STAFF_PASSWORD = "gate-pass-2026"


def setup_function(function):
    auth._FAILURES.clear()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    models.DB_PATH = path
    main.store = models.DataStore(db_path=path)
    main.store.create_user(OWNER_EMAIL, "Samyog Sapkota", OWNER_PASSWORD, role="owner")


def teardown_function(function):
    auth._FAILURES.clear()
    try:
        os.remove(models.DB_PATH)
    except OSError:
        pass


def sign_in(email=OWNER_EMAIL, password=OWNER_PASSWORD):
    return client.post("/api/admin/login", json={"email": email, "password": password})


def bearer(token):
    return {"Authorization": "Bearer " + token}


def owner_session():
    body = sign_in().json()
    return bearer(body["token"]), body["user"]


def staff_session():
    main.store.create_user(STAFF_EMAIL, "Gate staff", STAFF_PASSWORD, role="staff")
    body = sign_in(STAFF_EMAIL, STAFF_PASSWORD).json()
    return bearer(body["token"]), body["user"]


# --------------------------------------------------------------------------- #
# Sign in
# --------------------------------------------------------------------------- #
def test_sign_in_returns_a_session_and_the_user():
    body = sign_in().json()
    assert body["ok"] is True
    assert body["user"]["email"] == OWNER_EMAIL
    assert body["user"]["role"] == "owner"
    assert body["token"]
    assert body["expires_at"]

    res = client.get("/api/admin/session", headers=bearer(body["token"]))
    assert res.status_code == 200
    assert res.json()["user"]["id"] == body["user"]["id"]


def test_the_raw_token_is_never_stored():
    token = sign_in().json()["token"]
    conn = main.store._connect()
    try:
        rows = conn.execute("SELECT token_hash, user_agent FROM admin_sessions").fetchall()
    finally:
        conn.close()
    assert [row["token_hash"] for row in rows] == [auth.token_fingerprint(token)]
    assert token not in [row["token_hash"] for row in rows]


def test_email_is_case_insensitive():
    assert sign_in(OWNER_EMAIL.upper()).status_code == 200


def test_wrong_password_and_unknown_email_look_the_same():
    wrong = sign_in(password="not-the-password-1")
    unknown = sign_in("ghost@primeitclub.com", "not-the-password-1")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]
    assert wrong.headers.get("www-authenticate") == "Bearer"


def test_repeated_failures_are_throttled():
    for _ in range(5):
        assert sign_in("throttle@primeitclub.com", "wrong-password-1").status_code == 401
    blocked = sign_in("throttle@primeitclub.com", "wrong-password-1")
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0
    # The right password is refused too while the lockout runs.
    assert sign_in(OWNER_EMAIL, "prime-desk-2026").status_code == 200


def test_a_successful_sign_in_clears_earlier_failures():
    for _ in range(4):
        sign_in(password="wrong-password-1")
    assert sign_in().status_code == 200
    for _ in range(4):
        assert sign_in(password="wrong-password-1").status_code == 401


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #
def test_staff_can_read_the_floor_but_not_edit_events():
    headers, user = staff_session()
    assert user["role"] == "staff"

    assert client.get("/api/admin/overview", headers=headers).status_code == 200
    assert client.get("/api/admin/attendees", headers=headers).status_code == 200
    assert client.get("/api/admin/events", headers=headers).status_code == 200

    payload = {
        "title": "Staff Attempt",
        "category": "Workshop",
        "venue": "Hall",
        "capacity": 10,
        "starts_at": "2026-12-11T10:00:00+05:45",
        "ends_at": "2026-12-11T12:00:00+05:45",
    }
    created = client.post("/api/admin/events", headers=headers, json=payload)
    assert created.status_code == 403
    assert "owner" in created.json()["detail"].lower()
    assert client.post("/api/admin/selftest", headers=headers).status_code == 403
    assert client.get("/api/admin/users", headers=headers).status_code == 403


def test_a_staff_account_can_check_tickets_in():
    headers, _ = staff_session()
    event_id = client.get("/api/events", params={"scope": "upcoming"}).json()[0]["id"]
    token = client.post(
        "/api/register",
        json={
            "event_id": event_id,
            "student_name": "Door Test",
            "student_email": "door@example.com",
            "college_id": "CSIT-2080-123",
        },
    ).json()["ticket"]["token"]
    assert client.post("/api/check-in", params={"token": token}).status_code == 200
    feed = client.get("/api/admin/attendees", headers=headers, params={"status": "checked"}).json()
    assert feed["total"] == 1


# --------------------------------------------------------------------------- #
# Team management
# --------------------------------------------------------------------------- #
def test_an_owner_can_add_a_colleague():
    headers, _ = owner_session()
    res = client.post(
        "/api/admin/users",
        headers=headers,
        json={
            "email": "New.Coordinator@PrimeITClub.com",
            "name": "New Coordinator",
            "password": "coord-pass-2026",
            "role": "staff",
        },
    )
    assert res.status_code == 201, res.text
    created = res.json()
    assert created["email"] == "new.coordinator@primeitclub.com"
    assert created["role"] == "staff"
    assert "password" not in created and "password_hash" not in created

    listing = client.get("/api/admin/users", headers=headers).json()
    emails = [row["email"] for row in listing["items"]]
    assert emails == [OWNER_EMAIL, "new.coordinator@primeitclub.com"]
    assert sign_in("new.coordinator@primeitclub.com", "coord-pass-2026").status_code == 200


def test_duplicate_and_weak_accounts_are_refused():
    headers, _ = owner_session()
    duplicate = client.post(
        "/api/admin/users",
        headers=headers,
        json={"email": OWNER_EMAIL, "name": "Clone", "password": "another-pass-1", "role": "staff"},
    )
    assert duplicate.status_code == 409

    weak = client.post(
        "/api/admin/users",
        headers=headers,
        json={"email": "weak@primeitclub.com", "name": "Weak", "password": "short1", "role": "staff"},
    )
    assert weak.status_code == 422
    assert "10 characters" in weak.text

    bad_role = client.post(
        "/api/admin/users",
        headers=headers,
        json={
            "email": "role@primeitclub.com",
            "name": "Wrong Role",
            "password": "another-pass-1",
            "role": "superuser",
        },
    )
    assert bad_role.status_code == 422


def test_deactivating_an_account_ends_its_sessions():
    headers, _ = owner_session()
    staff_headers, staff = staff_session()
    assert client.get("/api/admin/session", headers=staff_headers).status_code == 200

    res = client.patch(
        f"/api/admin/users/{staff['id']}", headers=headers, json={"active": False}
    )
    assert res.status_code == 200
    assert res.json()["active"] is False

    assert client.get("/api/admin/session", headers=staff_headers).status_code == 401
    blocked = sign_in(STAFF_EMAIL, STAFF_PASSWORD)
    assert blocked.status_code == 403
    assert "deactivated" in blocked.json()["detail"]


def test_the_last_owner_cannot_lock_everybody_out():
    headers, owner = owner_session()
    demote = client.patch(f"/api/admin/users/{owner['id']}", headers=headers, json={"role": "staff"})
    assert demote.status_code == 400
    assert "last active owner" in demote.json()["detail"].lower()

    deactivate = client.patch(
        f"/api/admin/users/{owner['id']}", headers=headers, json={"active": False}
    )
    assert deactivate.status_code == 400

    # With a second owner in place the first one may step down.
    client.post(
        "/api/admin/users",
        headers=headers,
        json={
            "email": "second@primeitclub.com",
            "name": "Second Owner",
            "password": "second-pass-2026",
            "role": "owner",
        },
    )
    assert client.patch(
        f"/api/admin/users/{owner['id']}", headers=headers, json={"role": "staff"}
    ).status_code == 200


def test_an_owner_can_reset_a_colleagues_password():
    headers, _ = owner_session()
    staff_headers, staff = staff_session()
    res = client.patch(
        f"/api/admin/users/{staff['id']}", headers=headers, json={"password": "reset-pass-2026"}
    )
    assert res.status_code == 200
    assert client.get("/api/admin/session", headers=staff_headers).status_code == 401
    assert sign_in(STAFF_EMAIL, STAFF_PASSWORD).status_code == 401
    assert sign_in(STAFF_EMAIL, "reset-pass-2026").status_code == 200


# --------------------------------------------------------------------------- #
# Self service
# --------------------------------------------------------------------------- #
def test_changing_your_password_keeps_this_session_and_drops_the_others():
    headers, _ = owner_session()
    other = sign_in().json()["token"]

    wrong = client.post(
        "/api/admin/password",
        headers=headers,
        json={"current_password": "nope-nope-1", "new_password": "brand-new-pass-9"},
    )
    assert wrong.status_code == 400

    weak = client.post(
        "/api/admin/password",
        headers=headers,
        json={"current_password": OWNER_PASSWORD, "new_password": "short"},
    )
    assert weak.status_code == 422

    ok = client.post(
        "/api/admin/password",
        headers=headers,
        json={"current_password": OWNER_PASSWORD, "new_password": "brand-new-pass-9"},
    )
    assert ok.status_code == 200

    assert client.get("/api/admin/session", headers=headers).status_code == 200
    assert client.get("/api/admin/session", headers=bearer(other)).status_code == 401
    assert sign_in(password=OWNER_PASSWORD).status_code == 401
    assert sign_in(password="brand-new-pass-9").status_code == 200


def test_logout_revokes_only_that_session():
    headers, _ = owner_session()
    other = sign_in().json()["token"]
    assert client.post("/api/admin/logout", headers=headers).status_code == 200
    assert client.get("/api/admin/session", headers=headers).status_code == 401
    assert client.get("/api/admin/session", headers=bearer(other)).status_code == 200


def test_an_expired_session_is_refused():
    token = sign_in().json()["token"]
    conn = main.store._connect()
    try:
        conn.execute(
            "UPDATE admin_sessions SET expires_at = ? WHERE token_hash = ?",
            ("2020-01-01T00:00:00+00:00", auth.token_fingerprint(token)),
        )
        conn.commit()
    finally:
        conn.close()
    assert client.get("/api/admin/session", headers=bearer(token)).status_code == 401


def test_the_bootstrap_account_is_created_once_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_EMAIL", "bootstrap@primeitclub.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "bootstrap-pass-1")
    main.store = models.DataStore(db_path=str(tmp_path / "bootstrap.db"))

    message = main.bootstrap_admin()
    assert message and message.startswith("Created")
    assert main.bootstrap_admin() is None

    res = sign_in("bootstrap@primeitclub.com", "bootstrap-pass-1")
    assert res.status_code == 200
    assert res.json()["user"]["role"] == "owner"


def test_a_weak_bootstrap_password_does_not_crash_the_api(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_EMAIL", "weak-boot@primeitclub.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "short")
    main.store = models.DataStore(db_path=str(tmp_path / "weak.db"))
    message = main.bootstrap_admin()
    assert message and "Could not" in message
    assert main.store.user_count() == 0


# --------------------------------------------------------------------------- #
# Hashing
# --------------------------------------------------------------------------- #
def test_passwords_are_salted_and_verifiable():
    first = auth.hash_password("same-password-2026")
    second = auth.hash_password("same-password-2026")
    assert first != second
    assert first.startswith("pbkdf2_sha256$")
    assert auth.verify_password("same-password-2026", first)
    assert not auth.verify_password("same-password-2027", first)
    assert not auth.verify_password("same-password-2026", "garbage")
    assert not auth.verify_password("same-password-2026", "")


@pytest.mark.parametrize(
    "password,expected",
    [
        ("short1", "10 characters"),
        ("alllettershere", "number"),
        ("1234567890", "letter"),
        ("password123", "common"),
    ],
)
def test_password_policy_messages(password, expected):
    problem = auth.password_problem(password)
    assert problem and expected in problem
