import os
import sqlite3
import tempfile

from fastapi.testclient import TestClient

import main
import models
import qr_service
from main import app

client = TestClient(app)


def setup_function(function):
    """Use a fresh temp DB for each test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    models.DB_PATH = path
    main.store = models.DataStore(db_path=path)


def teardown_function(function):
    try:
        os.remove(models.DB_PATH)
    except OSError:
        pass


def register(event_id=1, name="Student", email="student@example.com", college="CSIT-2080-001"):
    return client.post(
        "/api/register",
        json={
            "event_id": event_id,
            "student_name": name,
            "student_email": email,
            "college_id": college,
        },
    )


def upcoming_event_id():
    events = client.get("/api/events", params={"scope": "upcoming"}).json()
    assert events, "catalog should contain an upcoming event"
    return events[0]["id"]


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #
def test_list_events_returns_the_club_catalog():
    res = client.get("/api/events")
    assert res.status_code == 200
    events = res.json()
    assert len(events) == len(models.CATALOG)
    titles = {e["title"] for e in events}
    assert "ICT Meetup V7.0" in titles
    assert "Hackaverse: Hack the Hassle" in titles
    assert "Prime IT Job Fair 2025" in titles


def test_events_carry_schedule_capacity_and_status():
    events = client.get("/api/events").json()
    sample = events[0]
    for field in ("slug", "starts_at", "ends_at", "seats_left", "status", "date", "time"):
        assert field in sample
    assert sample["status"] in {"upcoming", "live", "past"}
    assert sample["seats_left"] <= sample["capacity"]


def test_upcoming_scope_excludes_finished_events():
    events = client.get("/api/events", params={"scope": "upcoming"}).json()
    assert events
    assert all(e["status"] != "past" for e in events)
    assert all(e["status"] == "upcoming" for e in events)


def test_events_filter_by_category():
    events = client.get("/api/events", params={"category": "Hackathon"}).json()
    assert events
    assert {e["category"] for e in events} == {"Hackathon"}


def test_health_endpoint_hides_storage_diagnostics(monkeypatch):
    monkeypatch.setattr(
        main.store,
        "health",
        lambda: {
            "ok": False,
            "backend": "postgres",
            "target": "private-host/private-db",
            "error": "private connection detail",
        },
    )
    res = client.get("/api/health")
    assert res.status_code == 503
    assert res.json() == {"status": "unavailable"}


def test_ticket_qr_contains_only_the_server_verified_token(monkeypatch):
    payloads = []

    class FakeImage:
        def save(self, output, format):
            output.write(b"png")

    class FakeQrCode:
        def __init__(self, **kwargs):
            pass

        def add_data(self, value):
            payloads.append(value)

        def make(self, fit):
            pass

        def make_image(self, **kwargs):
            return FakeImage()

    monkeypatch.setattr(qr_service.qrcode, "QRCode", FakeQrCode)
    result = register(upcoming_event_id(), "Private Name", "private@example.com", "SECRET-ID").json()
    assert payloads == [result["ticket"]["token"]]


def test_root_serves_the_html_page():
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "Prime IT Club" in res.text


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def test_register_student_success_returns_qr():
    res = register(upcoming_event_id(), "Alice", "alice@example.com", "CSIT-001")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["success"] is True
    assert data["duplicate"] is False
    assert data["ticket"]["token"]
    assert data["ticket"]["ticket_id"].startswith("PRIME-")
    assert data["qr_image"].startswith("data:image/png;base64,")


def test_register_trims_whitespace_and_lowercases_email():
    res = register(upcoming_event_id(), "  Bob  ", "  Bob@Example.com  ", "  CSIT-002  ")
    assert res.status_code == 200
    ticket = res.json()["ticket"]
    assert ticket["student_name"] == "Bob"
    assert ticket["college_id"] == "CSIT-002"
    assert ticket["student_email"] == "bob@example.com"


def test_register_invalid_email_rejected():
    res = register(upcoming_event_id(), "Bad", "not-an-email", "CSIT-003")
    assert res.status_code == 422


def test_duplicate_registration_does_not_reissue_an_existing_ticket():
    event_id = upcoming_event_id()
    first = register(event_id, "Carol", "carol@example.com", "CSIT-004")
    second = register(event_id, "Carol", "carol@example.com", "CSIT-004")
    assert first.status_code == 200
    assert second.status_code == 409
    assert "saved pass" in second.json()["detail"]
    assert "token" not in second.json()
    assert "qr_image" not in second.json()


def test_register_unknown_event_404():
    res = register(9999, "Ghost", "ghost@example.com", "CSIT-005")
    assert res.status_code == 404


def test_register_at_capacity_rejected():
    conn = sqlite3.connect(models.DB_PATH)
    conn.execute(
        "INSERT INTO events (id, title, slug, category, date, time, venue, capacity, "
        "registered_count, speaker, description, starts_at, ends_at) "
        "VALUES (900, 'Tiny Session', 'tiny-session', 'Workshop', 'Nov 1, 2026', '10 AM', "
        "'Room', 1, 0, 'Speaker', 'Desc', '2026-11-01T10:00:00+05:45', "
        "'2026-11-01T12:00:00+05:45')"
    )
    conn.execute(
        "INSERT INTO tickets (token, ticket_id, event_id, event_title, student_name, "
        "student_email, college_id, is_checked_in, check_in_time, created_at) "
        "VALUES ('AAAA1111', 'PRIME-AAAA11', 900, 'Tiny Session', 'First', "
        "'first@example.com', 'C-1', 0, NULL, '2026-10-01 10:00:00')"
    )
    conn.commit()
    conn.close()

    res = register(900, "Late", "late@example.com", "CSIT-006")
    assert res.status_code == 409
    assert "capacity" in res.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# Check-in
# --------------------------------------------------------------------------- #
def test_check_in_requires_an_admin_session():
    token = register(upcoming_event_id(), "Dave", "dave@example.com", "CSIT-007").json()[
        "ticket"
    ]["token"]
    response = client.post("/api/check-in", params={"token": token})
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Stats and CSV
# --------------------------------------------------------------------------- #
def test_stats_require_an_admin_session():
    assert client.get("/api/stats").status_code == 401


def test_export_csv_requires_an_admin_session():
    response = client.get("/api/export.csv", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/api/admin/export.csv"
    assert client.get("/api/export.csv", follow_redirects=True).status_code == 401
