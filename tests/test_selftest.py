import os
import tempfile

from fastapi.testclient import TestClient

import main
import models
from main import app

client = TestClient(app)
SESSION = {}


def setup_function(function):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    models.DB_PATH = path
    main.store = models.DataStore(db_path=path)
    main.store.create_user("owner@primeitclub.com", "Owner", "prime-desk-2026", role="owner")
    res = client.post(
        "/api/admin/login",
        json={"email": "owner@primeitclub.com", "password": "prime-desk-2026"},
    )
    assert res.status_code == 200, res.text
    SESSION["owner"] = {"Authorization": "Bearer " + res.json()["token"]}


def teardown_function(function):
    try:
        os.remove(models.DB_PATH)
    except OSError:
        pass


def test_health_says_where_the_rows_live():
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["storage"]["backend"] == "sqlite"
    assert body["storage"]["durable"] is False
    assert body["storage"]["ok"] is True
    assert body["storage"]["counts"]["events"] >= 1
    assert body["auth"]["accounts"] == 1
    assert body["auth"]["configured"] is True


def test_health_says_when_no_admin_account_exists(tmp_path):
    main.store = models.DataStore(db_path=str(tmp_path / "empty.db"))
    body = client.get("/api/health").json()
    assert body["auth"] == {"configured": False, "accounts": 0, "ready": False}


def test_selftest_requires_a_session():
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
