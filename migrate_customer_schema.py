"""Apply the additive customer schema to an explicitly selected database."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import db

STAGING_CONFIRMATION = "I_CONFIRM_THIS_IS_DISPOSABLE_STAGING"


def _configured_url(variable: str) -> str:
    configured = {
        name: os.getenv(name, "").strip()
        for name in db.URL_ENV_VARS
        if os.getenv(name, "").strip()
    }
    if not configured:
        raise ValueError("No PostgreSQL database URL is configured.")
    if len(configured) != 1:
        raise ValueError(
            "Multiple database URL variables are configured; unset all but the selected staging URL."
        )
    if variable not in configured:
        raise ValueError(
            "The selected PostgreSQL URL variable must be the only configured database URL."
        )
    url = db.normalise_url(configured[variable])
    parts = urlsplit(url)
    if (
        parts.scheme != "postgresql"
        or not parts.hostname
        or not parts.path.strip("/")
    ):
        raise ValueError("The selected environment variable is not a valid PostgreSQL URL.")
    return url


def _validate_postgres_target(database: db.Database, expected_target: str) -> str:
    if database.dialect != "postgres":
        raise ValueError("The selected database is not PostgreSQL.")
    if not expected_target or database.target != expected_target:
        raise ValueError(
            "The configured PostgreSQL target does not match --expected-target."
        )
    database_name = unquote(urlsplit(database.url).path.strip("/"))
    if not database_name:
        raise ValueError("The PostgreSQL URL does not identify a database.")
    return database_name


def _require_initialized_postgres(database: db.Database, database_name: str) -> None:
    conn = database.connect()
    try:
        identity = conn.execute(
            "SELECT current_database() AS database_name"
        ).fetchone()
        if not identity or identity["database_name"] != database_name:
            raise ValueError(
                "The connected PostgreSQL database does not match the configured target."
            )
        for table in ("events", "tickets"):
            if not conn.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = ?",
                (table,),
            ).fetchone():
                raise ValueError(
                    "The selected PostgreSQL database is not an initialized ticket database."
                )
    finally:
        conn.close()


def _migrate_postgres(database: db.Database, expected_target: str) -> None:
    database_name = _validate_postgres_target(database, expected_target)
    _require_initialized_postgres(database, database_name)
    database.migrate_customer_schema()


def _migrate_sqlite(path_value: str) -> None:
    if any(os.getenv(name, "").strip() for name in db.URL_ENV_VARS):
        raise ValueError(
            "Unset all configured database URL variables before running a local SQLite migration."
        )
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise ValueError("The selected SQLite database file does not exist.")

    database = db.Database(url="", sqlite_path=str(path))
    conn = database.connect()
    try:
        for table in ("events", "tickets"):
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone():
                raise ValueError(
                    "The selected file is not an initialized ticket database."
                )
    finally:
        conn.close()
    database.migrate_customer_schema()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    backend = parser.add_mutually_exclusive_group(required=True)
    backend.add_argument(
        "--sqlite-path",
        help="Existing local SQLite database file.",
    )
    backend.add_argument(
        "--postgres-url-env",
        choices=db.URL_ENV_VARS,
        help="The sole configured PostgreSQL URL variable for an isolated staging database.",
    )
    parser.add_argument(
        "--expected-target",
        help="Expected non-secret PostgreSQL target in host:port/database form.",
    )
    parser.add_argument(
        "--confirm-staging",
        choices=(STAGING_CONFIRMATION,),
        help="Required explicit confirmation that the selected target is disposable staging.",
    )
    args = parser.parse_args()

    try:
        if args.sqlite_path:
            if args.expected_target or args.confirm_staging:
                raise ValueError("PostgreSQL confirmation options cannot be used with SQLite.")
            _migrate_sqlite(args.sqlite_path)
            print("Customer schema migration applied to the selected local SQLite database.")
            return 0

        if not args.expected_target or not args.confirm_staging:
            raise ValueError(
                "PostgreSQL migration requires --expected-target and explicit disposable-staging confirmation."
            )
        url = _configured_url(args.postgres_url_env)
        database = db.Database(url=url)
        _migrate_postgres(database, args.expected_target)
        print(
            "Customer schema migration applied to the explicitly confirmed PostgreSQL staging target."
        )
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    except Exception as exc:
        parser.error(
            "Database migration failed "
            f"({type(exc).__name__}); no success was reported."
        )


if __name__ == "__main__":
    sys.exit(main())
