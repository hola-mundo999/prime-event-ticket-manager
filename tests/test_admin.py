import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import main
import models
from main import app

client = TestClient(app)
OWNER_EMAIL = "owner@primeitclub.com"
OWNER_PASSWORD = "prime-desk-2026"
SESSION = {}


def setup_function(function):
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
    res = client.post("/api/admin/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.text
    return {"Authorization": "Bearer " + res.json()["token"]}


def auth():
    """Headers carrying the owner session created for this test."""
    return SESSION["owner"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/admin/session",
        "/api/admin/overview",
        "/api/admin/events",
        "/api/admin/attendees",
        "/api/admin/export.csv",
        "/api/admin/users",
    ],
)
def test_admin_routes_need_a_session(path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer made-up"}).status_code == 401


def test_admin_routes_reject_a_non_bearer_header():
    assert client.get("/api/admin/overview", headers={"x-admin-token": "anything"}).status_code == 401


def test_login_refuses_when_no_account_exists(tmp_path):
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
    assert body["expires_at"]


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

    client.post("/api/check-in", params={"token": token})
    checked = client.get(
        "/api/admin/attendees", headers=auth(), params={"status": "checked"}
    ).json()
    assert checked["total"] == 1
    assert checked["items"][0]["is_checked_in"] is True

    missing = client.get(
        "/api/admin/attendees", headers=auth(), params={"q": "nobody"}
    ).json()
    assert missing["total"] == 0


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
    assert "Scoped Student" not in client.get(
        "/api/admin/export.csv", headers=auth(), params={"event_id": 424242}
    ).text
