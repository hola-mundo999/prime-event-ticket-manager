import os
import sqlite3
import tempfile

from fastapi.testclient import TestClient

import main
import models
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


def test_health_endpoint():
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


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


def test_register_twice_returns_the_same_ticket():
    event_id = upcoming_event_id()
    first = register(event_id, "Carol", "carol@example.com", "CSIT-004")
    second = register(event_id, "Carol", "carol@example.com", "CSIT-004")
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["ticket"]["token"] == first.json()["ticket"]["token"]
    assert second.json()["duplicate"] is True


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
def test_check_in_valid_ticket():
    token = register(upcoming_event_id(), "Dave", "dave@example.com", "CSIT-007").json()[
        "ticket"
    ]["token"]
    res = client.post("/api/check-in", params={"token": token})
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["ticket"]["is_checked_in"] is True
    assert data["ticket"]["check_in_time"]


def test_check_in_accepts_lowercase_token():
    token = register(upcoming_event_id(), "Dana", "dana@example.com", "CSIT-008").json()[
        "ticket"
    ]["token"]
    res = client.post("/api/check-in", params={"token": token.lower()})
    assert res.status_code == 200


def test_check_in_invalid_token_404():
    res = client.post("/api/check-in", params={"token": "NOPE1234"})
    assert res.status_code == 404


def test_check_in_twice_is_conflict():
    token = register(upcoming_event_id(), "Eve", "eve@example.com", "CSIT-009").json()[
        "ticket"
    ]["token"]
    client.post("/api/check-in", params={"token": token})
    res = client.post("/api/check-in", params={"token": token})
    assert res.status_code == 409
    assert "already" in res.json()["detail"].lower()


def test_check_in_empty_token_404():
    res = client.post("/api/check-in", params={"token": ""})
    assert res.status_code == 404


# --------------------------------------------------------------------------- #
# Stats and CSV
# --------------------------------------------------------------------------- #
def test_stats_counts_registered_and_checked_in():
    event_id = upcoming_event_id()
    token = register(event_id, "Frank", "frank@example.com", "CSIT-010").json()["ticket"][
        "token"
    ]
    client.post("/api/check-in", params={"token": token})

    data = client.get("/api/stats").json()
    assert data["total_registered"] >= 1
    assert data["total_checked_in"] >= 1
    assert data["total_events"] == len(models.CATALOG) + 0 or data["total_events"] >= len(
        models.CATALOG
    )
    assert 0 <= data["attendance_rate"] <= 100
    assert "recent_tickets" in data


def test_export_csv_contains_every_ticket_not_just_recent():
    event_id = upcoming_event_id()
    for i in range(12):
        register(event_id, f"Student {i}", f"student{i}@example.com", f"CSIT-{100 + i}")
    res = client.get("/api/export.csv")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    rows = [line for line in res.text.strip().splitlines() if line]
    assert len(rows) == 13  # header + 12 tickets
    assert "student11@example.com" in res.text
