"""Storage adapter for the Prime IT Club ticket desk.

Local development runs on SQLite with no setup. Production runs on Postgres,
which is what Supabase gives you: durable rows that survive a redeploy, shared
by the public site and the admin console.

Everything in the app talks to one tiny interface, so the store is written once:

    conn = database.connect()
    conn.execute("SELECT * FROM events WHERE id = ?", (1,))
    conn.commit()
    conn.close()

On Postgres the ``?`` placeholders are rewritten to ``%s``, rows come back as
plain dicts and connections are pooled. On SQLite the file is used as before.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Sequence

#: Checked in order. Supabase's Vercel integration sets POSTGRES_URL, while a
#: manual Render setup usually sets DATABASE_URL.
URL_ENV_VARS = (
    "DATABASE_URL",
    "SUPABASE_DB_URL",
    "SUPABASE_DATABASE_URL",
    "POSTGRES_URL",
    "POSTGRES_URL_NON_POOLING",
)

SQLITE_PATH = os.getenv("TICKET_DB_PATH", "event_tickets.db")

# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
# Kept deliberately close between the two engines so one set of queries works.
# `is_checked_in` is a real boolean on Postgres and 0/1 on SQLite; every query
# compares against TRUE/FALSE, which both engines understand.
SCHEMA: Dict[str, List[str]] = {
    "sqlite": [
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            venue TEXT NOT NULL,
            capacity INTEGER NOT NULL,
            registered_count INTEGER NOT NULL DEFAULT 0,
            speaker TEXT NOT NULL,
            description TEXT NOT NULL,
            slug TEXT,
            starts_at TEXT,
            ends_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS tickets (
            token TEXT PRIMARY KEY,
            ticket_id TEXT NOT NULL,
            event_id INTEGER NOT NULL,
            event_title TEXT NOT NULL,
            student_name TEXT NOT NULL,
            student_email TEXT NOT NULL,
            college_id TEXT NOT NULL,
            is_checked_in INTEGER NOT NULL DEFAULT 0,
            check_in_time TEXT,
            created_at TEXT NOT NULL,
            created_ms TEXT,
            FOREIGN KEY (event_id) REFERENCES events(id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_tickets_event ON tickets(event_id)",
        "CREATE INDEX IF NOT EXISTS idx_tickets_email ON tickets(student_email)",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_events_slug ON events(slug)
        WHERE slug IS NOT NULL AND slug <> ''
        """,
        # Admin console accounts. Never holds a password, only a pbkdf2 hash.
        """
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'staff',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS admin_sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES admin_users(id),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            user_agent TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_sessions_user ON admin_sessions(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON admin_sessions(expires_at)",
    ],
    "postgres": [
        """
        CREATE TABLE IF NOT EXISTS events (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            venue TEXT NOT NULL,
            capacity INTEGER NOT NULL,
            registered_count INTEGER NOT NULL DEFAULT 0,
            speaker TEXT NOT NULL,
            description TEXT NOT NULL,
            slug TEXT,
            starts_at TEXT,
            ends_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS tickets (
            token TEXT PRIMARY KEY,
            ticket_id TEXT NOT NULL,
            event_id INTEGER NOT NULL REFERENCES events(id),
            event_title TEXT NOT NULL,
            student_name TEXT NOT NULL,
            student_email TEXT NOT NULL,
            college_id TEXT NOT NULL,
            is_checked_in BOOLEAN NOT NULL DEFAULT FALSE,
            check_in_time TEXT,
            created_at TEXT NOT NULL,
            created_ms TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_tickets_event ON tickets(event_id)",
        "CREATE INDEX IF NOT EXISTS idx_tickets_email ON tickets(student_email)",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_events_slug ON events(slug)
        WHERE slug IS NOT NULL AND slug <> ''
        """,
        """
        CREATE TABLE IF NOT EXISTS admin_users (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'staff',
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TEXT NOT NULL,
            last_login_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS admin_sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES admin_users(id),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            user_agent TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_sessions_user ON admin_sessions(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON admin_sessions(expires_at)",
    ],
}

#: Columns added after the first release, per table.
LATE_COLUMNS: Dict[str, Dict[str, Dict[str, str]]] = {
    "sqlite": {
        "events": {"slug": "TEXT", "starts_at": "TEXT", "ends_at": "TEXT"},
        "tickets": {"created_ms": "TEXT"},
    },
    "postgres": {
        "events": {"slug": "TEXT", "starts_at": "TEXT", "ends_at": "TEXT"},
        "tickets": {"created_ms": "TEXT"},
    },
}


def normalise_url(url: str) -> str:
    """Turn whatever the dashboard handed us into something psycopg accepts."""
    url = (url or "").strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url and "sslmode=" not in url and "localhost" not in url and "127.0.0.1" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


def database_url() -> str:
    """The first configured Postgres URL, or '' when running on SQLite."""
    for key in URL_ENV_VARS:
        value = os.getenv(key, "").strip()
        if value:
            return normalise_url(value)
    return ""


def dict_row_factory() -> Any:
    """psycopg's row factory that yields plain dicts.

    The store is written against SQLite's ``sqlite3.Row``, which behaves like a
    mapping. psycopg defaults to tuples, so every ``dict(row)`` in this file
    would explode with 'dictionary update sequence element #1 has length 7; 2
    is required' the moment the app boots on a real database.
    """
    from psycopg.rows import dict_row

    return dict_row


def describe_url(url: str) -> str:
    """Host and database only, so a health check can prove the target without
    ever exposing the password."""
    if not url:
        return "sqlite"
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        host = parts.hostname or "unknown-host"
        port = f":{parts.port}" if parts.port else ""
        database = (parts.path or "/").lstrip("/") or "postgres"
        return f"{host}{port}/{database}"
    except Exception:  # pragma: no cover - only to keep health checks alive
        return "configured"


class Result:
    """Just enough of a DB-API cursor for the store."""

    __slots__ = ("_rows", "rowcount", "lastrowid")

    def __init__(
        self,
        rows: Optional[Sequence[Dict[str, Any]]] = None,
        rowcount: int = 0,
        lastrowid: Optional[int] = None,
    ) -> None:
        self._rows = list(rows or [])
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchone(self) -> Optional[Dict[str, Any]]:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> List[Dict[str, Any]]:
        return list(self._rows)


class _SQLiteConnection:
    dialect = "sqlite"

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, timeout=15)
        self._conn.row_factory = sqlite3.Row

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Result:
        cursor = self._conn.execute(sql, tuple(params))
        rows: List[Dict[str, Any]] = []
        if cursor.description:
            rows = [dict(row) for row in cursor.fetchall()]
        return Result(rows, cursor.rowcount, cursor.lastrowid)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


class _PostgresConnection:
    dialect = "postgres"

    def __init__(self, connection: Any, pool: Any = None) -> None:
        self._conn = connection
        self._pool = pool

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Result:
        cursor = self._conn.execute(sql.replace("?", "%s"), tuple(params))
        rows: List[Dict[str, Any]] = []
        if cursor.description:
            names = [column[0] for column in cursor.description]
            fetched = cursor.fetchall()
            # Connections ask for dict_row, but a tuple row would otherwise be
            # fatal during boot, so map by column name as a second line of
            # defence before the row ever reaches the store.
            rows = [
                row
                if isinstance(row, dict)
                else dict(zip(names, tuple(row)))
                for row in fetched
            ]
        return Result(rows, cursor.rowcount)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        """Hand the connection back to the pool instead of tearing it down."""
        if self._pool is not None:
            try:
                self._conn.rollback()  # never return a half-open transaction
            except Exception:  # pragma: no cover - broken socket, pool will drop it
                pass
            self._pool.putconn(self._conn)
            return
        self._conn.close()


class Database:
    """One object per process; hands out connections and owns the schema."""

    def __init__(self, url: str = "", sqlite_path: str = SQLITE_PATH) -> None:
        self.url = normalise_url(url)
        self.sqlite_path = sqlite_path
        self.dialect = "postgres" if self.url else "sqlite"
        self._pool: Any = None
        self._lock = threading.Lock()

    # -- connections -------------------------------------------------------- #
    @property
    def target(self) -> str:
        return describe_url(self.url) if self.dialect == "postgres" else self.sqlite_path

    def _open_pool(self) -> Any:
        try:
            import psycopg  # noqa: F401
            from psycopg.rows import dict_row  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on the host
            raise RuntimeError(
                "DATABASE_URL is set but the Postgres driver is missing. "
                "Install it with: pip install 'psycopg[binary,pool]' "
                "(the requirements.txt in this repo already lists it)."
            ) from exc

        try:
            from psycopg_pool import ConnectionPool
        except ImportError:
            ConnectionPool = None  # type: ignore[assignment]

        # prepare_threshold=None keeps us compatible with Supabase's transaction
        # pooler (PgBouncer), which cannot hold server-side prepared statements.
        # row_factory makes every row a dict, as the store expects.
        settings = {
            "connect_timeout": 10,
            "prepare_threshold": None,
            "application_name": "prime-ticket-desk",
            "row_factory": dict_row,
        }
        if ConnectionPool is None:
            import psycopg

            return psycopg.connect(self.url, **settings)

        pool = ConnectionPool(
            conninfo=self.url,
            min_size=0,
            max_size=5,
            timeout=15,
            kwargs=settings,
            open=False,
        )
        pool.open(wait=False)
        return pool

    def pool(self) -> Any:
        if self._pool is None:
            with self._lock:
                if self._pool is None:
                    self._pool = self._open_pool()
        return self._pool

    def connect(self) -> Any:
        if self.dialect == "sqlite":
            return _SQLiteConnection(self.sqlite_path)
        pool = self.pool()
        if hasattr(pool, "getconn"):  # psycopg_pool.ConnectionPool
            return _PostgresConnection(pool.getconn(), pool=pool)
        import psycopg

        return _PostgresConnection(
            psycopg.connect(
                self.url,
                connect_timeout=10,
                prepare_threshold=None,
                row_factory=dict_row_factory(),
            )
        )

    def release(self, connection: Any) -> None:
        connection.close()

    # -- schema ------------------------------------------------------------- #
    def create_schema(self) -> None:
        conn = self.connect()
        try:
            for statement in SCHEMA[self.dialect]:
                conn.execute(statement)
            conn.commit()
        finally:
            self.release(conn)

    def columns(self, table: str) -> Dict[str, str]:
        conn = self.connect()
        try:
            if self.dialect == "sqlite":
                rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
                return {row["name"]: row.get("type", "") for row in rows}
            rows = conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = ?",
                (table,),
            ).fetchall()
            return {row["column_name"]: row["data_type"] for row in rows}
        finally:
            self.release(conn)

    def add_missing_columns(self) -> List[str]:
        """Bring a table created by an older release up to date."""
        added: List[str] = []
        expected = LATE_COLUMNS[self.dialect]
        for table, columns in expected.items():
            existing = self.columns(table)
            if not existing:
                continue
            for column, sql_type in columns.items():
                if column in existing:
                    continue
                conn = self.connect()
                try:
                    if self.dialect == "postgres":
                        conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {sql_type}"
                        )
                    else:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
                    conn.commit()
                    added.append(f"{table}.{column}")
                finally:
                    self.release(conn)
        return added

    # -- diagnostics -------------------------------------------------------- #
    def ping(self) -> bool:
        conn = self.connect()
        try:
            conn.execute("SELECT 1 AS ok").fetchone()
            return True
        finally:
            self.release(conn)

    def close(self) -> None:
        if self._pool is not None and hasattr(self._pool, "close"):
            self._pool.close()
            self._pool = None
