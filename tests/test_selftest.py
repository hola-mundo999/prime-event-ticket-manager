import os
import tempfile

from fastapi.testclient import TestClient

import main
import models
from main import app
from test_support import authorize_admin, csrf_headers

client = TestClient(app)
SESSION = {}


def setup_function(function):
    client.cookies.clear()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    models.DB_PATH = path
    main.store = models.DataStore(db_path=path)
    main.store.create_user(
        "samyog.sapkota1@gmail.com", "Owner", "prime-desk-2026", role="owner"
    )
    res = authorize_admin(client, password="prime-desk-2026")
    assert res.status_code == 200, res.text
    SESSION["owner"] = csrf_headers(client)


def teardown_function(function):
    try:
        os.remove(models.DB_PATH)
    except OSError:
        pass


def test_health_says_where_the_rows_live():
    response = client.get("/api/admin/health", headers=SESSION["owner"])
    assert response.status_code == 200
    storage = response.json()["storage"]
    assert storage["backend"] == "sqlite"
    assert storage["durable"] is False
    assert storage["ok"] is True
    assert storage["counts"]["events"] >= 1


def test_admin_health_requires_owner_session():
    client.cookies.clear()
    response = client.get("/api/admin/health")
    assert response.status_code == 401


def test_public_health_does_not_disclose_admin_presence(tmp_path):
    main.store = models.DataStore(db_path=str(tmp_path / "empty.db"))
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_selftest_requires_a_session():
    client.cookies.clear()
    assert client.post("/api/admin/selftest").status_code == 401


def test_selftest_walks_the_whole_flow_and_cleans_up():
    headers = SESSION["owner"]
    before = client.get("/api/admin/overview", headers=headers).json()["totals"]

    response = client.post("/api/admin/selftest", headers=headers)
    assert response.status_code == 200
    body = response.json()

    assert body["ok"] is True, body["steps"]
    assert [step["step"] for step in body["steps"]] == [
        "create event",
        "register pass",
        "duplicate guard",
        "gate check-in",
        "read back",
    ]
    assert all(step["ok"] for step in body["steps"])

    after = client.get("/api/admin/overview", headers=headers).json()["totals"]
    assert after == before, "the self-test must not leave events or passes behind"

    # The test event must not show up on the public page either.
    titles = [event["title"] for event in client.get("/api/events").json()]
    assert "Prime ticket self-test" not in titles
