import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

import auth
import customer_auth
import db
import main
import migrate_customer_schema
import models


PASSWORD = "Customer1234"


def run_concurrently(*operations):
    barrier = Barrier(len(operations))

    def invoke(operation):
        barrier.wait(timeout=10)
        return operation()

    with ThreadPoolExecutor(max_workers=len(operations)) as executor:
        futures = [executor.submit(invoke, operation) for operation in operations]
        return [future.result(timeout=30) for future in futures]


@pytest.fixture
def customer_app(tmp_path, monkeypatch):
    for name in db.URL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CUSTOMER_ACCOUNTS_ENABLED", "true")
    monkeypatch.setenv("RESEND_API_KEY", "test-only-not-a-real-key")
    monkeypatch.setenv(
        "RESEND_FROM_EMAIL",
        "Prime Event Ticket Manager <noreply@mail.samyog-sapkota.com.np>",
    )
    monkeypatch.setenv(
        "CUSTOMER_WEB_URL", "https://prime-event-ticket-manager.vercel.app"
    )
    store = models.DataStore(db_path=str(tmp_path / "customers.sqlite"))
    store.migrate_customer_schema()
    main.store = store
    sent = []

    def fake_send(recipient, subject, action, token):
        sent.append(
            {"recipient": recipient, "subject": subject, "action": action, "token": token}
        )

    monkeypatch.setattr(main, "send_customer_email", fake_send)
    client = TestClient(main.app)
    yield client, store, sent
    client.cookies.clear()


def register_customer(client, email="customer@example.com"):
    response = client.post(
        "/api/customer/register",
        json={"email": email, "display_name": "Customer Name"},
    )
    assert response.status_code == 202, response.text
    return response


def verify_customer(client, sent, email="customer@example.com", password=PASSWORD):
    register_customer(client, email)
    link = sent[-1]
    assert link["action"] == "verify_email"
    verified = client.post(
        "/api/customer/verify-email",
        json={"token": link["token"], "new_password": password},
    )
    assert verified.status_code == 200, verified.text


def login_customer(client, email="customer@example.com", password=PASSWORD):
    response = client.post(
        "/api/customer/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()["user"]


def customer_csrf(client):
    token = client.cookies.get("prime_customer_csrf")
    return {"X-Customer-CSRF-Token": token} if token else {}


def upcoming_event_id(store):
    return next(event.id for event in store.get_events() if event.seats_left > 0)


def test_customer_password_hash_uses_random_salts_and_secure_work_factor():
    first = customer_auth.hash_customer_password(PASSWORD)
    second = customer_auth.hash_customer_password(PASSWORD)
    assert first != second
    assert first.split("$")[0] == "pbkdf2_sha256"
    assert int(first.split("$")[1]) == 600_000
    assert customer_auth.verify_customer_password(PASSWORD, first)
    assert not customer_auth.verify_customer_password("wrong", first)
    assert not customer_auth.verify_customer_password(
        PASSWORD, "pbkdf2_sha256$999999999999$bad$bad"
    )


def test_customer_accounts_are_opt_in_and_schema_is_not_created_implicitly(
    tmp_path, monkeypatch
):
    for name in db.URL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CUSTOMER_ACCOUNTS_ENABLED", "false")
    store = models.DataStore(db_path=str(tmp_path / "disabled.sqlite"))
    main.store = store
    client = TestClient(main.app)

    assert client.get("/api/customer/availability").json() == {"enabled": False}
    assert not store.customer_schema_ready()
    assert (
        client.post(
            "/api/customer/register",
            json={
                "email": "customer@example.com",
                "display_name": "Customer",
            },
        ).status_code
        == 404
    )


def test_local_migration_cli_only_targets_sqlite_and_refuses_database_urls(
    tmp_path, monkeypatch
):
    for name in db.URL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "ticket-store.sqlite"
    models.DataStore(db_path=str(path))
    script = Path(__file__).resolve().parents[1] / "migrate_customer_schema.py"
    environment = os.environ.copy()

    migrated = subprocess.run(
        [sys.executable, str(script), "--sqlite-path", str(path)],
        cwd=str(script.parent),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert migrated.returncode == 0, migrated.stderr
    assert "local SQLite database" in migrated.stdout

    environment["DATABASE_URL"] = "not-a-real-production-url"
    refused = subprocess.run(
        [sys.executable, str(script), "--sqlite-path", str(path)],
        cwd=str(script.parent),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode != 0
    assert "Unset all configured database URL variables" in refused.stderr


def test_customer_schema_migration_is_explicit_idempotent_and_preserves_legacy_tickets(
    tmp_path, monkeypatch
):
    for name in db.URL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    store = models.DataStore(db_path=str(tmp_path / "legacy.sqlite"))
    event_id = upcoming_event_id(store)
    ticket, created = store.register_ticket(
        models.RegistrationRequest(
            event_id=event_id,
            student_name="Legacy Student",
            student_email="legacy@example.com",
            college_id="CSIT-LEGACY",
        )
    )
    assert ticket and created
    assert not store.customer_schema_ready()
    admin = store.create_user(auth.ADMIN_EMAIL, "Admin", "AdminPassword123", role="owner")
    conn = store._connect()
    try:
        conn.execute(
            "UPDATE admin_users SET mfa_enabled = TRUE WHERE id = ?", (admin["id"],)
        )
        conn.commit()
    finally:
        conn.close()
    admin_session = store.start_session(int(admin["id"]))

    store.migrate_customer_schema()
    store.migrate_customer_schema()

    assert store.customer_schema_ready()
    assert store.session_user(admin_session["token"])
    conn = store._connect()
    try:
        assert conn.execute("SELECT COUNT(*) AS c FROM tickets").fetchone()["c"] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM customer_ticket_ownership"
            ).fetchone()["c"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM customer_schema_migrations "
                "WHERE migration_key = ?",
                (customer_auth.CUSTOMER_MIGRATION_KEY,),
            ).fetchone()["c"]
            == 1
        )
        account_indexes = {
            row["name"]
            for row in conn.execute("PRAGMA index_list(customer_accounts)").fetchall()
        }
        assert any(
            index.startswith("sqlite_autoindex_customer_accounts")
            for index in account_indexes
        )
        session_indexes = {
            row["name"]
            for row in conn.execute("PRAGMA index_list(customer_sessions)").fetchall()
        }
        assert {
            "idx_customer_sessions_customer",
            "idx_customer_sessions_expiry",
        } <= session_indexes
        session_foreign_keys = conn.execute(
            "PRAGMA foreign_key_list(customer_sessions)"
        ).fetchall()
        assert any(
            row["table"] == "customer_accounts"
            and row["from"] == "customer_id"
            and row["on_delete"] == "CASCADE"
            for row in session_foreign_keys
        )
    finally:
        conn.close()


class FakePostgresCursor:
    def __init__(self, rows=(), description=None):
        self._rows = list(rows)
        self.description = (
            [(name,) for name in description] if description is not None else None
        )
        self.rowcount = 1

    def fetchall(self):
        return self._rows


class FakePostgresConnection:
    def __init__(self, fail_on=None):
        self.statements = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.fail_on = fail_on

    def execute(self, statement, params=()):
        self.statements.append((statement, tuple(params)))
        if self.fail_on and self.fail_on in statement:
            raise RuntimeError("mock PostgreSQL migration failure")
        if "SELECT current_database()" in statement:
            return FakePostgresCursor(
                [{"database_name": "customer_test"}], ["database_name"]
            )
        if "information_schema.tables" in statement:
            return FakePostgresCursor([{"exists": 1}], ["exists"])
        return FakePostgresCursor()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def _mock_postgres_connections(database, connections, monkeypatch):
    remaining = iter(connections)
    monkeypatch.setattr(
        database,
        "connect",
        lambda: db._PostgresConnection(next(remaining)),
    )


def test_postgres_migration_uses_mock_adapter_and_expected_ddl(monkeypatch):
    database = db.Database(url="postgresql://staging.invalid/customer_test")
    validation = FakePostgresConnection()
    migration = FakePostgresConnection()
    _mock_postgres_connections(database, [validation, migration], monkeypatch)

    migrate_customer_schema._migrate_postgres(database, database.target)

    statements = [sql for sql, _ in migration.statements]
    normalized = "\n".join(statements)
    assert validation.statements[0][0] == "SELECT current_database() AS database_name"
    assert any("%s" in sql for sql, _ in validation.statements)
    assert "BIGSERIAL PRIMARY KEY" in normalized
    assert "BIGINT NOT NULL REFERENCES customer_accounts(id) ON DELETE CASCADE" in normalized
    assert "ticket_token TEXT PRIMARY KEY REFERENCES tickets(token) ON DELETE CASCADE" in normalized
    assert "CREATE INDEX IF NOT EXISTS idx_customer_sessions_customer" in normalized
    assert "CREATE INDEX IF NOT EXISTS idx_customer_sessions_expiry" in normalized
    assert "email TEXT NOT NULL UNIQUE" in normalized
    assert "ON CONFLICT (migration_key) DO NOTHING" in normalized
    assert migration.commits == 1
    assert migration.rollbacks == 0
    assert validation.closed and migration.closed


def test_mocked_postgres_migration_is_idempotent_and_tracks_once(monkeypatch):
    database = db.Database(url="postgresql://staging.invalid/customer_test")
    connections = [FakePostgresConnection() for _ in range(4)]
    _mock_postgres_connections(database, connections, monkeypatch)

    migrate_customer_schema._migrate_postgres(database, database.target)
    migrate_customer_schema._migrate_postgres(database, database.target)

    first_migration = connections[1]
    second_migration = connections[3]
    for migration in (first_migration, second_migration):
        statements = [sql for sql, _ in migration.statements]
        assert sum("CREATE TABLE IF NOT EXISTS" in sql for sql in statements) == 6
        assert sum("CREATE INDEX IF NOT EXISTS" in sql for sql in statements) == 4
        tracking = [
            sql
            for sql in statements
            if "INSERT INTO customer_schema_migrations" in sql
        ]
        assert len(tracking) == 1
        assert "ON CONFLICT (migration_key) DO NOTHING" in tracking[0]
        assert migration.commits == 1


def test_postgres_migration_rolls_back_on_ddl_failure(monkeypatch):
    database = db.Database(url="postgresql://staging.invalid/customer_test")
    connection = FakePostgresConnection(
        fail_on="CREATE TABLE IF NOT EXISTS customer_action_tokens"
    )
    monkeypatch.setattr(
        database, "connect", lambda: db._PostgresConnection(connection)
    )

    with pytest.raises(RuntimeError, match="mock PostgreSQL migration failure"):
        database.migrate_customer_schema()

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.closed


def test_postgres_migration_rejects_ambiguous_or_unsupported_configuration(
    monkeypatch,
):
    monkeypatch.setenv("DATABASE_URL", "postgresql://staging.invalid/customer_test")
    monkeypatch.setenv("POSTGRES_URL", "postgresql://other.invalid/customer_test")
    with pytest.raises(ValueError, match="Multiple database URL variables"):
        migrate_customer_schema._configured_url("DATABASE_URL")

    monkeypatch.delenv("POSTGRES_URL")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///not-postgres.db")
    with pytest.raises(ValueError, match="not a valid PostgreSQL URL"):
        migrate_customer_schema._configured_url("DATABASE_URL")


def test_postgres_cli_requires_staging_confirmation_before_any_connection(
    monkeypatch, capsys
):
    monkeypatch.setenv("DATABASE_URL", "postgresql://staging.invalid/customer_test")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "migrate_customer_schema.py",
            "--postgres-url-env",
            "DATABASE_URL",
            "--expected-target",
            "staging.invalid/customer_test",
        ],
    )

    with pytest.raises(SystemExit) as error:
        migrate_customer_schema.main()

    assert error.value.code == 2
    assert "--confirm-staging" in capsys.readouterr().err


def test_postgres_migration_rejects_target_mismatch_before_connecting(monkeypatch):
    database = db.Database(url="postgresql://staging.invalid/customer_test")

    def unexpected_connect():
        raise AssertionError("target mismatch must fail before connecting")

    monkeypatch.setattr(database, "connect", unexpected_connect)
    with pytest.raises(ValueError, match="does not match --expected-target"):
        migrate_customer_schema._migrate_postgres(database, "production.invalid/customer")


def test_postgres_migration_refuses_wrong_connected_database(monkeypatch):
    database = db.Database(url="postgresql://staging.invalid/customer_test")
    connection = FakePostgresConnection()

    def wrong_database(statement, params=()):
        connection.statements.append((statement, tuple(params)))
        return FakePostgresCursor(
            [{"database_name": "different_database"}], ["database_name"]
        )

    connection.execute = wrong_database
    monkeypatch.setattr(
        database, "connect", lambda: db._PostgresConnection(connection)
    )

    with pytest.raises(ValueError, match="does not match the configured target"):
        migrate_customer_schema._require_initialized_postgres(database, "customer_test")
    assert connection.closed


def test_postgres_migration_uses_mocked_connection_and_postgres_ddl(monkeypatch):
    class FakeConnection:
        def __init__(self):
            self.statements = []
            self.rowcount = 1

        def execute(self, statement, params=()):
            self.statements.append((statement, tuple(params)))
            return self

        def commit(self):
            pass

        def rollback(self):
            raise AssertionError("successful mocked migration should not roll back")

        def close(self):
            pass

    database = db.Database(url="postgresql://localhost/customer_test")
    connection = FakeConnection()
    monkeypatch.setattr(database, "connect", lambda: connection)

    database.migrate_customer_schema()

    statements = [sql for sql, _ in connection.statements]
    assert any("BIGSERIAL PRIMARY KEY" in sql for sql in statements)
    assert any("customer_sessions" in sql and "BIGINT" in sql for sql in statements)
    assert any("customer_ticket_ownership" in sql for sql in statements)
    assert any(
        "INSERT INTO customer_schema_migrations" in sql and "ON CONFLICT" in sql
        for sql in statements
    )
    assert not any("admin_sessions" in sql or "admin_users" in sql for sql in statements)


def test_customer_authentication_isolated_from_admin_auth(customer_app):
    client, store, sent = customer_app
    verify_customer(client, sent)
    customer_email = "customer@example.com"
    assert store.find_user(customer_email) is None
    login_response = client.post(
        "/api/customer/login",
        json={"email": customer_email, "password": PASSWORD},
    )
    assert login_response.status_code == 200
    assert "token" not in login_response.json()
    session_token = client.cookies.get("prime_customer_session")
    csrf_token = client.cookies.get("prime_customer_csrf")
    conn = store._connect()
    try:
        stored_session = conn.execute(
            "SELECT token_hash, csrf_hash FROM customer_sessions WHERE token_hash = ?",
            (auth.token_fingerprint(session_token),),
        ).fetchone()
        assert stored_session
        assert stored_session["token_hash"] != session_token
        assert stored_session["csrf_hash"] != csrf_token
    finally:
        conn.close()

    assert client.get("/api/customer/session").status_code == 200
    assert client.get("/api/admin/session").status_code == 401
    assert client.get("/api/admin/users").status_code == 401

    client.cookies.clear()
    admin = store.create_user(auth.ADMIN_EMAIL, "Admin", "AdminPassword123", role="owner")
    conn = store._connect()
    try:
        conn.execute(
            "UPDATE admin_users SET mfa_enabled = TRUE WHERE id = ?", (admin["id"],)
        )
        conn.commit()
    finally:
        conn.close()
    admin_session = store.start_session(int(admin["id"]))
    client.cookies.set(
        "prime_admin_session", admin_session["token"], path="/api"
    )

    assert client.get("/api/admin/session").status_code == 200
    assert client.get("/api/customer/session").status_code == 401
    assert store.customer_by_email(auth.ADMIN_EMAIL) is None


def test_customer_login_and_password_change_rotate_sessions_and_csrf_is_separate(
    customer_app,
):
    client, store, sent = customer_app
    verify_customer(client, sent)
    login_customer(client)
    first_token = client.cookies.get("prime_customer_session")
    assert first_token
    assert client.cookies.get("prime_admin_session") is None

    login_customer(client)
    rotated_login_token = client.cookies.get("prime_customer_session")
    assert rotated_login_token and rotated_login_token != first_token
    assert store.customer_session_user(first_token) is None
    assert client.patch(
        "/api/customer/profile", json={"display_name": "Updated"}
    ).status_code == 403

    profile = client.patch(
        "/api/customer/profile",
        json={"display_name": "Updated Name"},
        headers=customer_csrf(client),
    )
    assert profile.status_code == 200
    assert profile.json()["user"]["display_name"] == "Updated Name"

    changed = client.post(
        "/api/customer/password",
        json={
            "current_password": PASSWORD,
            "new_password": "NewTicket5678",
        },
        headers=customer_csrf(client),
    )
    assert changed.status_code == 200, changed.text
    new_token = client.cookies.get("prime_customer_session")
    assert new_token and new_token != rotated_login_token
    assert store.customer_session_user(rotated_login_token) is None
    assert client.get("/api/customer/session").status_code == 200


def test_password_recovery_is_single_use_and_revokes_all_sessions(customer_app):
    client, store, sent = customer_app
    verify_customer(client, sent)
    login_customer(client)
    old_session = client.cookies.get("prime_customer_session")

    requested = client.post(
        "/api/customer/password/forgot",
        json={"email": "customer@example.com"},
    )
    assert requested.status_code == 202
    reset_link = sent[-1]
    assert reset_link["action"] == "password_reset"
    reset = client.post(
        "/api/customer/password/reset",
        json={"token": reset_link["token"], "new_password": "Recovered5678"},
    )
    assert reset.status_code == 200
    assert store.customer_session_user(old_session) is None
    assert (
        client.post(
            "/api/customer/password/reset",
            json={"token": reset_link["token"], "new_password": "Replay56789"},
        ).status_code
        == 400
    )
    login_customer(client, password="Recovered5678")


def test_registration_cannot_bind_an_attacker_chosen_password_to_unverified_email(
    customer_app,
):
    client, _store, sent = customer_app
    attacker_password = "Attacker12345"
    owner_password = "OwnerChosen5678"
    response = client.post(
        "/api/customer/register",
        json={
            "email": "victim@example.com",
            "display_name": "Victim",
            "password": attacker_password,
        },
    )
    assert response.status_code == 202
    verification_token = sent[-1]["token"]
    assert (
        client.post(
            "/api/customer/verify-email",
            json={"token": verification_token},
        ).status_code
        == 422
    )

    verified = client.post(
        "/api/customer/verify-email",
        json={
            "token": verification_token,
            "new_password": owner_password,
        },
    )
    assert verified.status_code == 200, verified.text
    assert (
        client.post(
            "/api/customer/login",
            json={"email": "victim@example.com", "password": attacker_password},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/customer/login",
            json={"email": "victim@example.com", "password": owner_password},
        ).status_code
        == 200
    )


def test_password_change_invalidates_outstanding_password_reset_tokens(customer_app):
    client, store, sent = customer_app
    verify_customer(client, sent)
    login_customer(client)

    requested = client.post(
        "/api/customer/password/forgot",
        json={"email": "customer@example.com"},
    )
    assert requested.status_code == 202
    reset_token = sent[-1]["token"]

    changed = client.post(
        "/api/customer/password",
        json={
            "current_password": PASSWORD,
            "new_password": "ChangedPassword5678",
        },
        headers=customer_csrf(client),
    )
    assert changed.status_code == 200, changed.text
    assert (
        client.post(
            "/api/customer/password/reset",
            json={
                "token": reset_token,
                "new_password": "AttackerPassword5678",
            },
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/customer/login",
            json={
                "email": "customer@example.com",
                "password": "ChangedPassword5678",
            },
        ).status_code
        == 200
    )


def test_password_change_racing_reset_redemption_has_one_consistent_winner(customer_app):
    client, store, sent = customer_app
    verify_customer(client, sent)
    customer_id = int(store.customer_by_email("customer@example.com")["id"])
    reset_token = store.create_customer_action(
        customer_id, "password_reset", timedelta(minutes=30)
    )

    change_result, reset_result = run_concurrently(
        lambda: store.change_customer_password(
            customer_id, PASSWORD, "ChangedPassword5678"
        ),
        lambda: store.reset_customer_password(reset_token, "RecoveredPassword5678"),
    )

    account = store.customer_by_email("customer@example.com")
    if change_result[0] == "ok":
        assert reset_result is False
        assert customer_auth.verify_customer_password(
            "ChangedPassword5678", account["password_hash"]
        )
    else:
        assert change_result == ("wrong", None)
        assert reset_result is True
        assert customer_auth.verify_customer_password(
            "RecoveredPassword5678", account["password_hash"]
        )
    assert not customer_auth.verify_customer_password(PASSWORD, account["password_hash"])


def test_two_concurrent_attempts_cannot_redeem_the_same_reset_token(customer_app):
    client, store, sent = customer_app
    verify_customer(client, sent)
    customer_id = int(store.customer_by_email("customer@example.com")["id"])
    reset_token = store.create_customer_action(
        customer_id, "password_reset", timedelta(minutes=30)
    )

    first, second = run_concurrently(
        lambda: store.reset_customer_password(reset_token, "FirstRecovery5678"),
        lambda: store.reset_customer_password(reset_token, "SecondRecovery5678"),
    )

    assert sorted((first, second)) == [False, True]
    account = store.customer_by_email("customer@example.com")
    assert sum(
        customer_auth.verify_customer_password(password, account["password_hash"])
        for password in ("FirstRecovery5678", "SecondRecovery5678")
    ) == 1


def test_concurrent_verification_token_issue_and_consumption_are_single_use(
    customer_app,
):
    client, store, sent = customer_app
    register_customer(client)
    customer_id = int(store.customer_by_email("customer@example.com")["id"])
    initial_token = sent[-1]["token"]

    issued_token, consumed = run_concurrently(
        lambda: store.create_customer_action(
            customer_id, "verify_email", timedelta(hours=24)
        ),
        lambda: store.verify_customer_email(initial_token, "OwnerPassword5678"),
    )

    assert issued_token != initial_token
    assert isinstance(consumed, bool)
    assert store.verify_customer_email(initial_token, "ReplayPassword5678") is False
    conn = store._connect()
    try:
        active = conn.execute(
            "SELECT token_hash FROM customer_action_tokens WHERE customer_id = ? "
            "AND purpose = 'verify_email' AND consumed_at IS NULL",
            (customer_id,),
        ).fetchall()
    finally:
        conn.close()
    assert [row["token_hash"] for row in active] == [
        auth.token_fingerprint(issued_token)
    ]


def test_concurrent_rate_limit_updates_preserve_every_attempt(customer_app):
    _client, store, _sent = customer_app
    attempts = 24
    results = run_concurrently(
        *(
            lambda: store.use_customer_rate_limit(
                "login:ip:concurrent-test", "login", maximum=7
            )
            for _ in range(attempts)
        )
    )

    assert sum(results) == 7
    conn = store._connect()
    try:
        row = conn.execute(
            "SELECT attempts FROM customer_auth_limits WHERE bucket_hash = ?",
            (auth.token_fingerprint("login:ip:concurrent-test"),),
        ).fetchone()
    finally:
        conn.close()
    assert row["attempts"] == attempts


def test_rate_limit_capacity_cleanup_removes_expired_before_preserving_active(
    customer_app, monkeypatch
):
    _client, store, _sent = customer_app
    monkeypatch.setattr(customer_auth, "MAX_TRACKED_CUSTOMER_LIMITS", 3)
    monkeypatch.setattr(customer_auth, "CUSTOMER_LIMIT_CLEANUP_BATCH", 1)
    now = customer_auth._now()
    conn = store._connect()
    try:
        for bucket, started_at in (
            ("expired", customer_auth._iso(now - timedelta(hours=1))),
            ("active-one", customer_auth._iso(now)),
            ("active-two", customer_auth._iso(now)),
        ):
            conn.execute(
                "INSERT INTO customer_auth_limits "
                "(bucket_hash, operation, window_started_at, attempts) "
                "VALUES (?, 'login', ?, 3)",
                (bucket, started_at),
            )
        conn.commit()
    finally:
        conn.close()

    assert store.use_customer_rate_limit("new", "login", maximum=5)
    conn = store._connect()
    try:
        rows = conn.execute(
            "SELECT bucket_hash, attempts FROM customer_auth_limits ORDER BY bucket_hash"
        ).fetchall()
    finally:
        conn.close()
    assert {row["bucket_hash"] for row in rows} == {
        "active-one",
        "active-two",
        auth.token_fingerprint("new"),
    }
    assert {row["attempts"] for row in rows if row["bucket_hash"].startswith("active-")} == {3}


def test_rate_limit_capacity_fails_closed_without_evicting_active_counters(
    customer_app, monkeypatch
):
    client, store, _sent = customer_app
    monkeypatch.setattr(customer_auth, "MAX_TRACKED_CUSTOMER_LIMITS", 2)
    now = customer_auth._iso(customer_auth._now())
    conn = store._connect()
    try:
        for bucket in ("active-one", "active-two"):
            conn.execute(
                "INSERT INTO customer_auth_limits "
                "(bucket_hash, operation, window_started_at, attempts) "
                "VALUES (?, 'login', ?, 4)",
                (bucket, now),
            )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(customer_auth.CustomerRateLimitStorageFull):
        store.use_customer_rate_limit("new", "login", maximum=5)

    blocked = client.post(
        "/api/customer/login",
        json={"email": "customer@example.com", "password": PASSWORD},
    )
    assert blocked.status_code == 503
    conn = store._connect()
    try:
        rows = conn.execute(
            "SELECT bucket_hash, attempts FROM customer_auth_limits ORDER BY bucket_hash"
        ).fetchall()
    finally:
        conn.close()
    assert [(row["bucket_hash"], row["attempts"]) for row in rows] == [
        ("active-one", 4),
        ("active-two", 4),
    ]


def test_concurrent_new_rate_limit_buckets_cannot_exceed_capacity(
    customer_app, monkeypatch
):
    _client, store, _sent = customer_app
    monkeypatch.setattr(customer_auth, "MAX_TRACKED_CUSTOMER_LIMITS", 2)
    now = customer_auth._iso(customer_auth._now())
    conn = store._connect()
    try:
        conn.execute(
            "INSERT INTO customer_auth_limits "
            "(bucket_hash, operation, window_started_at, attempts) "
            "VALUES ('active', 'login', ?, 1)",
            (now,),
        )
        conn.commit()
    finally:
        conn.close()

    def request_bucket(bucket):
        try:
            return store.use_customer_rate_limit(bucket, "login", maximum=5)
        except customer_auth.CustomerRateLimitStorageFull:
            return "full"

    results = run_concurrently(
        lambda: request_bucket("new-one"),
        lambda: request_bucket("new-two"),
    )

    assert results.count(True) == 1
    assert results.count("full") == 1
    conn = store._connect()
    try:
        rows = conn.execute(
            "SELECT bucket_hash FROM customer_auth_limits ORDER BY bucket_hash"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2
    assert "active" in {row["bucket_hash"] for row in rows}


def test_legacy_ticket_claim_requires_email_confirmation_and_is_single_use(customer_app):
    client, store, sent = customer_app
    ticket_response = client.post(
        "/api/register",
        json={
            "event_id": upcoming_event_id(store),
            "student_name": "Ticket Owner",
            "student_email": "customer@example.com",
            "college_id": "CSIT-CLAIM-1",
        },
    )
    assert ticket_response.status_code == 200
    ticket = ticket_response.json()["ticket"]

    verify_customer(client, sent)
    login_customer(client)
    conn = store._connect()
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM customer_ticket_ownership"
            ).fetchone()["c"]
            == 0
        )
    finally:
        conn.close()

    claim = client.post(
        "/api/customer/tickets/claim",
        json={"ticket_id": ticket["ticket_id"]},
        headers=customer_csrf(client),
    )
    assert claim.status_code == 202
    confirmation = sent[-1]
    assert confirmation["action"] == "ticket_claim"
    assert confirmation["recipient"] == "customer@example.com"
    customer_id = int(store.customer_by_email("customer@example.com")["id"])
    assert store.customer_tickets(customer_id) == []
    conn = store._connect()
    try:
        action = conn.execute(
            "SELECT created_at, expires_at FROM customer_action_tokens "
            "WHERE token_hash = ?",
            (auth.token_fingerprint(confirmation["token"]),),
        ).fetchone()
        assert datetime.fromisoformat(action["expires_at"]) - datetime.fromisoformat(
            action["created_at"]
        ) == timedelta(minutes=15)
    finally:
        conn.close()

    conn = store._connect()
    try:
        conn.execute(
            "UPDATE customer_action_tokens SET expires_at = '2000-01-01T00:00:00+00:00' "
            "WHERE token_hash = ?",
            (auth.token_fingerprint(confirmation["token"]),),
        )
        conn.commit()
    finally:
        conn.close()
    expired = client.post(
        "/api/customer/tickets/claim/confirm",
        json={"token": confirmation["token"]},
    )
    assert expired.status_code == 400

    retry = client.post(
        "/api/customer/tickets/claim",
        json={"ticket_id": ticket["ticket_id"]},
        headers=customer_csrf(client),
    )
    assert retry.status_code == 202
    confirmation = sent[-1]
    confirmed = client.post(
        "/api/customer/tickets/claim/confirm",
        json={"token": confirmation["token"]},
    )
    assert confirmed.status_code == 200
    assert (
        client.post(
            "/api/customer/tickets/claim/confirm",
            json={"token": confirmation["token"]},
        ).status_code
        == 400
    )
    tickets = client.get("/api/customer/tickets").json()["items"]
    assert [item["ticket_id"] for item in tickets] == [ticket["ticket_id"]]
    assert "student_email" not in tickets[0]
    assert "college_id" not in tickets[0]


def test_ticket_matching_email_alone_does_not_claim_and_new_booking_links_transactionally(
    customer_app,
):
    client, store, sent = customer_app
    event_id = upcoming_event_id(store)
    existing, created = store.register_ticket(
        models.RegistrationRequest(
            event_id=event_id,
            student_name="Original",
            student_email="someone-else@example.com",
            college_id="CSIT-SAME",
        )
    )
    assert existing and created

    verify_customer(client, sent)
    login_customer(client)
    sent.clear()
    response = client.post(
        "/api/customer/tickets/claim",
        json={"ticket_id": existing.ticket_id},
        headers=customer_csrf(client),
    )
    assert response.status_code == 202
    assert sent == []

    new_ticket = client.post(
        "/api/register",
        json={
            "event_id": event_id,
            "student_name": "Customer Name",
            "student_email": "customer@example.com",
            "college_id": "CSIT-NEW",
        },
        headers=customer_csrf(client),
    )
    assert new_ticket.status_code == 200
    owned = store.customer_tickets(int(store.customer_by_email("customer@example.com")["id"]))
    assert [item["ticket_id"] for item in owned] == [
        new_ticket.json()["ticket"]["ticket_id"]
    ]

    other = TestClient(main.app)
    other_registration = other.post(
        "/api/customer/register",
        json={
            "email": "other@example.com",
            "display_name": "Other Customer",
        },
    )
    assert other_registration.status_code == 202
    verification = sent[-1]
    assert other.post(
        "/api/customer/verify-email",
        json={"token": verification["token"], "new_password": PASSWORD},
    ).status_code == 200
    login_customer(other, "other@example.com")
    assert other.get("/api/customer/tickets").json()["items"] == []


def test_verified_customer_registration_with_different_email_does_not_link_ticket(
    customer_app,
):
    client, store, sent = customer_app
    verify_customer(client, sent)
    login_customer(client)

    response = client.post(
        "/api/register",
        json={
            "event_id": upcoming_event_id(store),
            "student_name": "Different Email",
            "student_email": "another-person@example.com",
            "college_id": "CSIT-DIFFERENT-EMAIL",
        },
        headers=customer_csrf(client),
    )

    assert response.status_code == 200
    ticket = response.json()["ticket"]
    assert ticket["student_email"] == "another-person@example.com"
    assert store.customer_tickets(
        int(store.customer_by_email("customer@example.com")["id"])
    ) == []
    conn = store._connect()
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM customer_ticket_ownership "
                "WHERE ticket_token = ?",
                (ticket["token"],),
            ).fetchone()["c"]
            == 0
        )
    finally:
        conn.close()


def test_resend_email_builds_fixed_frontend_url_and_keeps_api_key_out_of_content(
    monkeypatch,
):
    calls = []

    class Response:
        def raise_for_status(self):
            pass

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setenv("RESEND_API_KEY", "secret-test-value")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "events@example.com")
    monkeypatch.setenv("CUSTOMER_WEB_URL", "https://tickets.example.com")
    monkeypatch.setattr(customer_auth.httpx, "post", fake_post)

    customer_auth.send_customer_email(
        "a@example.com", "Verify", "verify_email", "one-time-token"
    )

    url, options = calls[0]
    assert url == "https://api.resend.com/emails"
    assert options["json"]["to"] == ["a@example.com"]
    assert "https://tickets.example.com/?customer_action=verify_email&token=one-time-token" in options["json"]["text"]
    assert "secret-test-value" not in str(options["json"])
    assert options["headers"]["Authorization"] == "Bearer secret-test-value"
