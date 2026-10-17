import os
import tempfile

from fastapi.testclient import TestClient

import models
import main
from main import app


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


client = TestClient(app)


# --------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------- #
def test_list_events_returns_three_seed_events():
    res = client.get("/api/events")
    assert res.status_code == 200
    events = res.json()
    assert len(events) == 3
    assert events[0]["id"] == 1
    assert events[0]["title"] == "AI & LLM Hands-on Workshop"


def test_get_event_not_found_returns_404():
    # via register with nonexistent event
    res = client.post("/api/register", json={
        "event_id": 999,
        "student_name": "Test Student",
        "student_email": "test@example.com",
        "college_id": "CSIT-1",
    })
    assert res.status_code == 404


# --------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------- #
def test_register_student_success_returns_qr():
    res = client.post("/api/register", json={
        "event_id": 1,
        "student_name": "Alice",
        "student_email": "alice@example.com",
        "college_id": "CSIT-2080-001",
    })
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["success"] is True
    assert data["ticket"]["token"]
    assert data["ticket"]["ticket_id"].startswith("PRIME-")
    assert data["qr_image"].startswith("data:image/png;base64,")


def test_register_student_trims_whitespace():
    res = client.post("/api/register", json={
        "event_id": 1,
        "student_name": "  Bob  ",
        "student_email": "  bob@example.com  ",
        "college_id": "  CSIT-002  ",
    })
    assert res.status_code == 200
    ticket = res.json()["ticket"]
    assert ticket["student_name"] == "Bob"
    assert ticket["college_id"] == "CSIT-002"


def test_register_invalid_email_rejected():
    res = client.post("/api/register", json={
        "event_id": 1,
        "student_name": "Bad",
        "student_email": "not-an-email",
        "college_id": "CSIT-003",
    })
    assert res.status_code == 422


def test_register_duplicate_email_college_returns_existing_ticket():
    r1 = client.post("/api/register", json={
        "event_id": 1,
        "student_name": "Carol",
        "student_email": "carol@example.com",
        "college_id": "CSIT-004",
    })
    assert r1.status_code == 200
    token = r1.json()["ticket"]["token"]

    r2 = client.post("/api/register", json={
        "event_id": 1,
        "student_name": "Carol Updated",
        "student_email": "carol@example.com",
        "college_id": "CSIT-004",
    })
    assert r2.status_code == 200
    assert r2.json()["ticket"]["token"] == token


def test_register_at_capacity_rejected():
    # Fill event 2 (capacity 80, registered 35) — can't easily fill, so test with a
    # freshly seeded tiny event via direct DB insert.
    import sqlite3
    conn = sqlite3.connect(models.DB_PATH)
    conn.execute(
        "INSERT INTO events (id, title, category, date, time, venue, capacity, registered_count, speaker, description) "
        "VALUES (9, 'Tiny Event', 'Workshop', 'Nov 1', '10 AM', 'Room', 1, 1, 'Speaker', 'Desc')"
    )
    conn.commit()
    conn.close()
    res = client.post("/api/register", json={
        "event_id": 9,
        "student_name": "Late",
        "student_email": "late@example.com",
        "college_id": "CSIT-009",
    })
    assert res.status_code == 409, res.text


# --------------------------------------------------------------------- #
# Check-in
# --------------------------------------------------------------------- #
def test_check_in_valid_ticket():
    reg = client.post("/api/register", json={
        "event_id": 2,
        "student_name": "Dave",
        "student_email": "dave@example.com",
        "college_id": "CSIT-005",
    })
    token = reg.json()["ticket"]["token"]

    res = client.post("/api/check-in", params={"token": token})
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["ticket"]["is_checked_in"] is True
    assert data["ticket"]["check_in_time"]


def test_check_in_invalid_token_404():
    res = client.post("/api/check-in", params={"token": "NOPE1234"})
    assert res.status_code == 404


def test_check_in_duplicate_409():
    reg = client.post("/api/register", json={
        "event_id": 3,
        "student_name": "Eve",
        "student_email": "eve@example.com",
        "college_id": "CSIT-006",
    })
    token = reg.json()["ticket"]["token"]
    client.post("/api/check-in", params={"token": token})

    res = client.post("/api/check-in", params={"token": token})
    assert res.status_code == 409
    assert res.json()["detail"].startswith("⚠️")


def test_check_in_empty_token_404():
    res = client.post("/api/check-in", params={"token": ""})
    assert res.status_code == 404


# --------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------- #
def test_stats_counts_registered_and_checked_in():
    client.post("/api/register", json={
        "event_id": 1,
        "student_name": "Frank",
        "student_email": "frank@example.com",
        "college_id": "CSIT-007",
    })
    res = client.get("/api/stats")
    assert res.status_code == 200
    data = res.json()
    assert data["total_registered"] >= 1
    assert "recent_tickets" in data


# --------------------------------------------------------------------- #
# CSV export
# --------------------------------------------------------------------- #
def test_export_csv_returns_text_csv():
    client.post("/api/register", json={
        "event_id": 1,
        "student_name": "Grace",
        "student_email": "grace@example.com",
        "college_id": "CSIT-008",
    })
    res = client.get("/api/export.csv")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert "ticket_id" in res.text
    assert "Grace" in res.text
