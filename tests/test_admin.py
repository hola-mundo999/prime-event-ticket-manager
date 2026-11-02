import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import main
import models
from main import app
from test_support import authorize_admin, csrf_headers

client = TestClient(app)
OWNER_EMAIL = "samyog.sapkota1@gmail.com"
OWNER_PASSWORD = "prime-desk-2026"
SESSION = {}


def setup_function(function):
    client.cookies.clear()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    models.DB_PATH = path
    main.store = models.DataStore(db_path=path)
    main.store.create_user(OWNER_EMAIL, "Samyog Sapkota", OWNER_PASSWORD, role="owner")
    SESSION["owner"] = sign_in(OWNER_EMAIL, OWNER_PASSWORD)


def teardown_function(function):
    try:
        os.remove(models.DB_PATH)
    except OSError:
        pass


def sign_in(email, password):
    res = authorize_admin(client, email, password)
    assert res.status_code == 200, res.text
    return csrf_headers(client)


def auth():
    """Headers carrying the owner session created for this test."""
    return SESSION["owner"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/admin/session",
        "/api/admin/overview",
        "/api/stats",
        "/api/admin/events",
        "/api/admin/attendees",
        "/api/admin/export.csv",
        "/api/export.csv",
        "/api/admin/users",
    ],
)
def test_admin_routes_need_a_session(path):
    client.cookies.clear()
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer made-up"}).status_code == 401


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/api/admin/logout", None),
        ("POST", "/api/admin/password", {
            "current_password": OWNER_PASSWORD,
            "new_password": "updated-admin-password-1",
        }),
        ("PATCH", "/api/admin/profile", {"name": "Changed Admin"}),
        ("POST", "/api/admin/users", {
            "email": "other@example.com",
            "name": "Other Admin",
            "password": "other-admin-password-1",
            "role": "owner",
        }),
        ("PATCH", "/api/admin/users/1", {"name": "Changed Admin"}),
        ("POST", "/api/admin/events", {
            "title": "Auth Test",
            "category": "Workshop",
            "venue": "Hall",
            "capacity": 10,
            "starts_at": "2026-12-11T10:00:00+05:45",
            "ends_at": "2026-12-11T12:00:00+05:45",
        }),
        ("PATCH", "/api/admin/events/1", {"title": "Changed"}),
        ("DELETE", "/api/admin/events/1", None),
        ("POST", "/api/admin/undo-check-in?token=NOPE1234", None),
        ("POST", "/api/admin/selftest", None),
    ],
)
def test_every_protected_admin_mutation_requires_a_full_session(method, path, body):
    client.cookies.clear()
    response = client.request(method, path, json=body)
    assert response.status_code == 401, response.text


def test_admin_routes_reject_a_non_bearer_header():
    client.cookies.clear()
    assert client.get("/api/admin/overview", headers={"x-admin-token": "anything"}).status_code == 401


def test_login_refuses_when_no_account_exists(tmp_path):
    client.cookies.clear()
    main.store = models.DataStore(db_path=str(tmp_path / "empty.db"))
    res = client.post(
        "/api/admin/login", json={"email": "nobody@primeitclub.com", "password": "some-password-1"}
    )
    assert res.status_code == 503
    assert "ADMIN_EMAIL" in res.json()["detail"]


def test_admin_session_returns_the_signed_in_user():
    res = client.get("/api/admin/session", headers=auth())
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["user"]["email"] == OWNER_EMAIL
    assert body["user"]["role"] == "owner"
    assert body["user"]["active"] is True
    assert body["user"]["preferences"] == {}
    assert body["user"]["created_at"]
    assert body["user"]["last_login_at"]
    assert body["expires_at"]


def test_admin_profile_updates_name_and_persisted_preferences():
    response = client.patch(
        "/api/admin/profile",
        headers=auth(),
        json={"name": "Updated Admin", "preferences": {"theme": "dark", "compact_mode": True}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["user"]["name"] == "Updated Admin"
    assert response.json()["user"]["preferences"] == {
        "theme": "dark",
        "compact_mode": True,
    }

    # A fresh session read verifies the settings were persisted in the isolated SQLite DB.
    session = client.get("/api/admin/session", headers=auth())
    assert session.status_code == 200
    assert session.json()["user"]["preferences"] == {
        "theme": "dark",
        "compact_mode": True,
    }

    partial = client.patch(
        "/api/admin/profile", headers=auth(), json={"preferences": {"theme": "light"}}
    )
    assert partial.status_code == 200
    assert partial.json()["user"]["preferences"] == {
        "theme": "light",
        "compact_mode": False,
    }
    name_only = client.patch(
        "/api/admin/profile", headers=auth(), json={"name": "Renamed Admin"}
    )
    assert name_only.status_code == 200
    assert name_only.json()["user"]["preferences"] == {
        "theme": "light",
        "compact_mode": False,
    }


def test_admin_profile_rejects_empty_or_invalid_preference_updates():
    empty = client.patch("/api/admin/profile", headers=auth(), json={})
    invalid = client.patch(
        "/api/admin/profile",
        headers=auth(),
        json={"preferences": {"theme": "neon", "compact_mode": True}},
    )
    assert empty.status_code == 422
    assert invalid.status_code == 422


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #
def test_overview_reports_totals_and_per_event_numbers():
    main.store.create_event(
        models.EventInput(
            title="Test Workshop",
            category="Workshop",
            venue="Lab 1",
            capacity=5,
            speaker="Speaker",
            description="Desc",
            starts_at="2026-12-01T10:00:00+05:45",
            ends_at="2026-12-01T12:00:00+05:45",
        )
    )
    data = client.get("/api/admin/overview", headers=auth()).json()
    assert data["totals"]["events"] == len(models.CATALOG) + 1
    assert "attendance_rate" in data["totals"]
    assert any(row["title"] == "Test Workshop" for row in data["events"])


# --------------------------------------------------------------------------- #
# Event CRUD
# --------------------------------------------------------------------------- #
def test_create_event():

    res = client.post(
        "/api/admin/events",
        headers=auth(),
        json={
            "title": "Cloud Computing Clinic",
            "category": "Workshop",
            "venue": "Lab 2",
            "capacity": 40,
            "speaker": "Cloud team",
            "description": "Hands-on clinic.",
            "starts_at": "2026-12-20T10:00:00+05:45",
            "ends_at": "2026-12-20T13:00:00+05:45",
        },
    )
    assert res.status_code == 201, res.text
    created = res.json()
    assert created["title"] == "Cloud Computing Clinic"
    assert created["slug"] == "cloud-computing-clinic"
    assert created["status"] == "upcoming"
    assert created["seats_left"] == 40
    assert created["date"] == "Dec 20, 2026"


def test_create_event_validates_input():
    res = client.post(
        "/api/admin/events",
        headers=auth(),
        json={
            "title": "x",
            "category": "",
            "venue": "",
            "capacity": 0,
            "starts_at": "not-a-date",
        },
    )
    assert res.status_code == 422


def test_update_event_changes_fields_and_regenerates_labels():
    created = client.post(
        "/api/admin/events",
        headers=auth(),
        json={
            "title": "Old Name",
            "category": "Session",
            "venue": "Hall",
            "capacity": 30,
            "starts_at": "2026-12-02T10:00:00+05:45",
            "ends_at": "2026-12-02T12:00:00+05:45",
        },
    ).json()

    res = client.patch(
        f"/api/admin/events/{created['id']}",
        headers=auth(),
        json={"title": "New Name", "capacity": 55, "starts_at": "2026-12-03T09:00:00+05:45"},
    )
    assert res.status_code == 200, res.text
    updated = res.json()
    assert updated["title"] == "New Name"
    assert updated["capacity"] == 55
    assert updated["date"] == "Dec 03, 2026"
    assert updated["time"].startswith("9:00 AM")


@pytest.mark.parametrize(
    "patch",
    [
        {"starts_at": "not-a-date"},
        {"starts_at": ""},
        {"ends_at": "2026-13-40T10:00:00+05:45"},
        {
            "starts_at": "2026-12-03T11:00:00+05:45",
            "ends_at": "2026-12-03T10:00:00+05:45",
        },
    ],
)
def test_update_event_rejects_invalid_schedule(patch):
    created = client.post(
        "/api/admin/events",
        headers=auth(),
        json={
            "title": "Schedule Validation",
            "category": "Session",
            "venue": "Hall",
            "capacity": 30,
            "starts_at": "2026-12-02T10:00:00+05:45",
            "ends_at": "2026-12-02T12:00:00+05:45",
        },
    ).json()
    response = client.patch(
        f"/api/admin/events/{created['id']}", headers=auth(), json=patch
    )
    assert response.status_code == 422


def test_update_event_rejects_end_before_start_when_only_end_changes():
    created = client.post(
        "/api/admin/events",
        headers=auth(),
        json={
            "title": "Partial Schedule Validation",
            "category": "Session",
            "venue": "Hall",
            "capacity": 30,
            "starts_at": "2026-12-02T10:00:00+05:45",
            "ends_at": "2026-12-02T12:00:00+05:45",
        },
    ).json()
    response = client.patch(
        f"/api/admin/events/{created['id']}",
        headers=auth(),
        json={"ends_at": "2026-12-02T09:00:00+05:45"},
    )
    assert response.status_code == 422


def test_update_unknown_event_404():
    res = client.patch("/api/admin/events/9999", headers=auth(), json={"title": "Nope"})
    assert res.status_code == 404


def test_delete_event_removes_its_tickets():
    created = client.post(
        "/api/admin/events",
        headers=auth(),
        json={
            "title": "Doomed Session",
            "category": "Session",
            "venue": "Hall",
            "capacity": 10,
            "starts_at": "2026-12-04T10:00:00+05:45",
            "ends_at": "2026-12-04T11:00:00+05:45",
        },
    ).json()

    client.post(
        "/api/register",
        json={
            "event_id": created["id"],
            "student_name": "Doomed Student",
            "student_email": "doomed@example.com",
            "college_id": "CSIT-999",
        },
    )
    res = client.delete(f"/api/admin/events/{created['id']}", headers=auth())
    assert res.status_code == 200
    assert res.json() == {"events": 1, "tickets": 1}
    assert client.delete(f"/api/admin/events/{created['id']}", headers=auth()).status_code == 404


# --------------------------------------------------------------------------- #
# Attendees
# --------------------------------------------------------------------------- #
def test_attendees_filter_by_event_search_and_status():
    event_id = client.get("/api/events", params={"scope": "upcoming"}).json()[0]["id"]
    token = client.post(
        "/api/register",
        json={
            "event_id": event_id,
            "student_name": "Rita Sharma",
            "student_email": "rita@example.com",
            "college_id": "CSIT-2080-777",
        },
    ).json()["ticket"]["token"]

    everything = client.get("/api/admin/attendees", headers=auth()).json()
    assert everything["total"] == 1
    first_page = client.get(
        "/api/admin/attendees", headers=auth(), params={"limit": 1, "offset": 0}
    ).json()
    assert first_page["total"] == 1
    assert len(first_page["items"]) == 1
    assert client.get(
        "/api/admin/attendees", headers=auth(), params={"limit": 0}
    ).status_code == 422
    assert client.get(
        "/api/admin/attendees", headers=auth(), params={"offset": -1}
    ).status_code == 422

    by_event = client.get(
        "/api/admin/attendees", headers=auth(), params={"event_id": event_id}
    ).json()
    assert by_event["total"] == 1

    by_search = client.get(
        "/api/admin/attendees", headers=auth(), params={"q": "rita"}
    ).json()
    assert by_search["total"] == 1

    pending = client.get(
        "/api/admin/attendees", headers=auth(), params={"status": "pending"}
    ).json()
    assert pending["total"] == 1

    client.post("/api/check-in", params={"token": token}, headers=auth())
    checked = client.get(
        "/api/admin/attendees", headers=auth(), params={"status": "checked"}
    ).json()
    assert checked["total"] == 1
    assert checked["items"][0]["is_checked_in"] is True

    missing = client.get(
        "/api/admin/attendees", headers=auth(), params={"q": "nobody"}
    ).json()
    assert missing["total"] == 0


def test_attendee_offset_pages_keep_total_and_do_not_overlap():
    event_id = client.get("/api/events", params={"scope": "upcoming"}).json()[0]["id"]
    tokens = []
    for name, email, college in (
        ("Page One", "page-one@example.com", "PAGE-001"),
        ("Page Two", "page-two@example.com", "PAGE-002"),
    ):
        response = client.post(
            "/api/register",
            json={
                "event_id": event_id,
                "student_name": name,
                "student_email": email,
                "college_id": college,
            },
        )
        tokens.append(response.json()["ticket"]["token"])

    first = client.get(
        "/api/admin/attendees", headers=auth(), params={"limit": 1, "offset": 0}
    ).json()
    second = client.get(
        "/api/admin/attendees", headers=auth(), params={"limit": 1, "offset": 1}
    ).json()
    beyond = client.get(
        "/api/admin/attendees", headers=auth(), params={"limit": 1, "offset": 2}
    ).json()

    assert first["total"] == second["total"] == beyond["total"] == 2
    assert len(first["items"]) == len(second["items"]) == 1
    assert first["items"][0]["token"] != second["items"][0]["token"]
    assert {first["items"][0]["token"], second["items"][0]["token"]} == set(tokens)
    assert beyond["items"] == []


def test_admin_export_can_be_scoped_to_one_event():
    event_id = client.get("/api/events", params={"scope": "upcoming"}).json()[0]["id"]
    client.post(
        "/api/register",
        json={
            "event_id": event_id,
            "student_name": "Scoped Student",
            "student_email": "scoped@example.com",
            "college_id": "CSIT-2080-888",
        },
    )
    res = client.get(
        "/api/admin/export.csv", headers=auth(), params={"event_id": event_id}
    )
    assert res.status_code == 200
    assert "Scoped Student" in res.text
    client.post(
        "/api/register",
        json={
            "event_id": event_id,
            "student_name": "Different Student",
            "student_email": "different@example.com",
            "college_id": "CSIT-2080-889",
        },
    )
    filtered = client.get(
        "/api/admin/export.csv",
        headers=auth(),
        params={"event_id": event_id, "q": "Scoped Student", "status": "pending"},
    )
    assert filtered.status_code == 200
    assert "Scoped Student" in filtered.text
    assert "Different Student" not in filtered.text
    legacy_route = client.get(
        "/api/export.csv",
        headers=auth(),
        params={"event_id": event_id},
        follow_redirects=True,
    )
    assert legacy_route.status_code == 200, legacy_route.text
    assert "Scoped Student" in legacy_route.text
    assert "Scoped Student" not in client.get(
        "/api/export.csv",
        headers=auth(),
        params={"event_id": 424242},
        follow_redirects=True,
    ).text
    assert "Scoped Student" not in client.get(
        "/api/admin/export.csv", headers=auth(), params={"event_id": 424242}
    ).text


# --------------------------------------------------------------------------- #
# Authenticated gate check-in
# --------------------------------------------------------------------------- #
def registered_token(name="Door Test", email="door@example.com", college="CSIT-2080-777"):
    event_id = client.get("/api/events", params={"scope": "upcoming"}).json()[0]["id"]
    res = client.post(
        "/api/register",
        json={
            "event_id": event_id,
            "student_name": name,
            "student_email": email,
            "college_id": college,
        },
    )
    assert res.status_code == 200, res.text
    return res.json()["ticket"]["token"]


def test_check_in_needs_an_admin_session():
    token = registered_token()
    client.cookies.clear()
    response = client.post("/api/check-in", params={"token": token})
    assert response.status_code == 401


def test_check_in_accepts_authenticated_admin_with_csrf():
    token = registered_token()
    response = client.post("/api/check-in", params={"token": token}, headers=auth())
    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
    assert response.json()["ticket"]["is_checked_in"] is True


def test_check_in_cookie_session_requires_csrf():
    token = registered_token()
    response = client.post("/api/check-in", params={"token": token})
    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# Undo a mis-scan at the door
# --------------------------------------------------------------------------- #
def checked_in_token(name="Door Test", email="door@example.com", college="CSIT-2080-777"):
    """Issue a pass, walk it through the gate, and hand back its token."""
    event_id = client.get("/api/events", params={"scope": "upcoming"}).json()[0]["id"]
    res = client.post(
        "/api/register",
        json={
            "event_id": event_id,
            "student_name": name,
            "student_email": email,
            "college_id": college,
        },
    )
    assert res.status_code == 200, res.text
    token = res.json()["ticket"]["token"]
    assert client.post("/api/check-in", params={"token": token}, headers=auth()).status_code == 200
    return token


def test_undo_check_in_needs_a_session():
    token = checked_in_token()
    client.cookies.clear()
    assert client.post("/api/admin/undo-check-in", params={"token": token}).status_code == 401


def test_undo_check_in_reverses_it_and_reopens_the_pass():
    token = checked_in_token()
    res = client.post("/api/admin/undo-check-in", params={"token": token}, headers=auth())
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["success"] is True
    assert data["ticket"]["is_checked_in"] is False
    assert data["ticket"]["check_in_time"] is None

    # the same pass has to work at the door again
    again = client.post("/api/check-in", params={"token": token}, headers=auth())
    assert again.status_code == 200
    assert again.json()["ticket"]["is_checked_in"] is True


def test_undoing_the_same_check_in_twice_is_a_conflict():
    token = checked_in_token()
    assert (
        client.post("/api/admin/undo-check-in", params={"token": token}, headers=auth()).status_code
        == 200
    )
    again = client.post("/api/admin/undo-check-in", params={"token": token}, headers=auth())
    assert again.status_code == 409
    assert "not been checked in" in again.json()["detail"]


def test_undo_on_an_unknown_token_is_404():
    res = client.post(
        "/api/admin/undo-check-in", params={"token": "NOPE1234"}, headers=auth()
    )
    assert res.status_code == 404


def test_undo_keeps_the_checked_in_totals_honest():
    token = checked_in_token()
    before = client.get("/api/stats", headers=auth()).json()["total_checked_in"]
    client.post("/api/admin/undo-check-in", params={"token": token}, headers=auth())
    assert client.get("/api/stats", headers=auth()).json()["total_checked_in"] == before - 1
