import os
import tempfile

from fastapi.testclient import TestClient

import main
import models
from main import app

client = TestClient(app)
TOKEN = "test-admin-token"


def setup_function(function):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    models.DB_PATH = path
    main.store = models.DataStore(db_path=path)
    main.ADMIN_TOKEN = TOKEN


def teardown_function(function):
    main.ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
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


def test_selftest_requires_the_admin_token():
    assert client.post("/api/admin/selftest").status_code == 401


def test_selftest_walks_the_whole_flow_and_cleans_up():
    headers = {"x-admin-token": TOKEN}
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
