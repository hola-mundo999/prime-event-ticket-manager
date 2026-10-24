"""Postgres-path tests.

There is no Postgres server in the test environment, so the driver is replaced
with a recorder. That still exercises everything that can silently break on the
hosted database: URL normalising, ``?`` -> ``%s`` rewriting, dict rows, the
Postgres DDL, and the advisory-lock / FOR UPDATE writes. The statements the
store itself builds are the shared subset that the SQLite suite already runs, so
anything that works here is dialect-safe SQL.
"""

import db
import models


class FakeCursor:
    def __init__(self, rows, rowcount=0):
        self._rows = list(rows)
        self.description = [("col",)] if rows is not None else None
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakePGConnection:
    """Answers the handful of shapes the store sends, and logs every one."""

    def __init__(self, log):
        self.log = log
        self.closed = False
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True

    def execute(self, sql, params=()):
        self.log.append((sql, params))
        flat = " ".join(sql.split())

        if flat.startswith("SELECT 1"):
            return FakeCursor([{"ok": 1}])
        if "information_schema.columns" in flat:
            table = params[0]
            columns = {
                "events": ["id", "title", "category", "date", "time", "venue",
                           "capacity", "registered_count", "speaker",
                           "description", "slug", "starts_at", "ends_at"],
                "tickets": ["token", "ticket_id", "event_id", "event_title",
                            "student_name", "student_email", "college_id",
                            "is_checked_in", "check_in_time", "created_at",
                            "created_ms"],
            }.get(table, [])
            return FakeCursor(
                [{"column_name": name, "data_type": "text"} for name in columns]
            )
        if "FROM tickets WHERE token" in flat:
            return FakeCursor(
                [
                    {
                        "token": "ABC123",
                        "ticket_id": "PRIME-ABC123",
                        "event_id": 1,
                        "event_title": "ICT Meetup V8.0",
                        "student_name": "Sita Sharma",
                        "student_email": "sita@example.com",
                        "college_id": "CSIT-2080-001",
                        "is_checked_in": False,
                        "check_in_time": None,
                        "created_at": "2026-10-24 10:00:00",
                        "created_ms": "2026-10-24 10:00:00.000",
                    }
                ]
            )
        if "COUNT(*) AS c FROM events" in flat:
            return FakeCursor([{"c": 0}])
        if "COUNT(*) AS c FROM tickets" in flat:
            return FakeCursor([{"c": 0}])
        if flat.startswith("SELECT id, starts_at FROM events"):
            return FakeCursor([])
        if flat.startswith("SELECT id FROM events"):
            return FakeCursor([])
        return FakeCursor([])


class FakePool:
    def __init__(self, log):
        self.log = log
        self.returned = 0

    def getconn(self):
        return FakePGConnection(self.log)

    def putconn(self, connection):
        self.returned += 1


def make_store(monkeypatch):
    log = []
    monkeypatch.setattr(db.Database, "_open_pool", lambda self: FakePool(log))
    store = models.DataStore(url="postgresql://postgres.ref:pw@aws-0-ap-south-1.pooler.supabase.com:6543/postgres")
    return store, log


def test_url_is_normalised_for_the_pooler():
    url = db.normalise_url("postgres://user:pw@host:6543/postgres")
    assert url.startswith("postgresql://")
    assert "sslmode=require" in url

    already = db.normalise_url("postgresql://user:pw@host:5432/postgres?sslmode=require")
    assert already.count("sslmode=") == 1


def test_localhost_does_not_force_tls():
    assert "sslmode" not in db.normalise_url("postgresql://postgres:pw@localhost:5432/postgres")


def test_first_configured_url_wins(monkeypatch):
    for key in db.URL_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("POSTGRES_URL", "postgres://a:b@pooler.example.com:6543/postgres")
    assert "a:b@pooler.example.com" in db.database_url()


def test_secrets_are_not_in_the_reported_target():
    database = db.Database(url="postgresql://postgres.ref:sup3rsecret@aws-0-ap-south-1.pooler.supabase.com:6543/postgres")
    assert database.dialect == "postgres"
    assert "sup3rsecret" not in database.target
    assert "aws-0-ap-south-1.pooler.supabase.com" in database.target


def test_postgres_bootstrap_uses_postgres_ddl_and_no_sqlite_syntax(monkeypatch):
    store, log = make_store(monkeypatch)

    assert store.backend == "postgres"
    sql_seen = " ".join(statement for statement, _ in log)
    assert "SERIAL PRIMARY KEY" in sql_seen
    assert "BOOLEAN NOT NULL DEFAULT FALSE" in sql_seen
    assert "AUTOINCREMENT" not in sql_seen
    assert "PRAGMA" not in sql_seen
    assert "rowid" not in sql_seen.lower()
    assert "BEGIN IMMEDIATE" not in sql_seen
    # Every placeholder must have been rewritten for psycopg.
    for statement, _ in log:
        assert "?" not in statement, statement


def test_check_in_locks_the_row_and_writes_a_boolean(monkeypatch):
    store, log = make_store(monkeypatch)
    result = store.check_in("abc123")

    assert result["success"] is True
    statements = [" ".join(statement.split()) for statement, _ in log]
    assert any(
        s.startswith("SELECT pg_advisory_xact_lock(hashtext(%s))") for s in statements
    )
    assert any("FROM tickets WHERE token = %s FOR UPDATE" in s for s in statements)
    assert any("is_checked_in = TRUE" in s for s in statements)


def test_health_reports_the_backend(monkeypatch):
    store, _ = make_store(monkeypatch)
    health = store.health()
    assert health["backend"] == "postgres"
    assert health["durable"] is True
    assert health["ok"] is True
