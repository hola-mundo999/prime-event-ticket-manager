"""Customer accounts, sessions, action tokens, and ticket ownership.

Customer credentials and sessions are deliberately separate from administrator
authentication. This mixin uses the DataStore connection adapter but never
reads or writes administrator account/session tables.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, EmailStr, Field, field_validator

from auth import password_problem, token_fingerprint

CUSTOMER_PASSWORD_ROUNDS = 600_000
CUSTOMER_SESSION_HOURS = 12
EMAIL_VERIFY_HOURS = 24
PASSWORD_RESET_MINUTES = 30
TICKET_CLAIM_MINUTES = 15
CUSTOMER_MIGRATION_KEY = "customer_accounts_v1"
MAX_LOGIN_ATTEMPTS = 10
MAX_ACTION_ATTEMPTS = 5
RATE_WINDOW_SECONDS = 15 * 60
MAX_TRACKED_CUSTOMER_LIMITS = 10_000
CUSTOMER_LIMIT_CLEANUP_BATCH = 100


class CustomerRateLimitStorageFull(RuntimeError):
    """Raised when the rate-limit table is full of counters that are still active."""


class CustomerRegister(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=120)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return str(value).strip().lower()

    @field_validator("display_name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Display name is required.")
        return value


class CustomerLogin(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return str(value).strip().lower()


class CustomerEmailRequest(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return str(value).strip().lower()


class CustomerTokenRequest(BaseModel):
    token: str = Field(min_length=20, max_length=256)


class CustomerEmailVerification(BaseModel):
    token: str = Field(min_length=20, max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


class CustomerProfilePatch(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)

    @field_validator("display_name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Display name is required.")
        return value


class CustomerPasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


class CustomerPasswordReset(BaseModel):
    token: str = Field(min_length=20, max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


class CustomerTicketClaim(BaseModel):
    ticket_id: str = Field(min_length=1, max_length=40)

    @field_validator("ticket_id")
    @classmethod
    def clean_ticket_id(cls, value: str) -> str:
        return value.strip()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def hash_customer_password(password: str) -> str:
    """Hash with PBKDF2-SHA256, a unique 128-bit salt, and 600,000 rounds."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, CUSTOMER_PASSWORD_ROUNDS
    )
    return "pbkdf2_sha256${}${}${}".format(
        CUSTOMER_PASSWORD_ROUNDS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_customer_password(password: str, stored: str) -> bool:
    """Verify a customer password without allowing stored parameters to DoS login."""
    try:
        scheme, rounds_text, salt_text, digest_text = (stored or "").split("$")
        rounds = int(rounds_text)
        if scheme != "pbkdf2_sha256" or not 100_000 <= rounds <= 2_000_000:
            return False
        salt = base64.b64decode(salt_text.encode("ascii"), validate=True)
        expected = base64.b64decode(digest_text.encode("ascii"), validate=True)
        if len(salt) != 16 or len(expected) != hashlib.sha256().digest_size:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, rounds
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, UnicodeError):
        return False


_DUMMY_PASSWORD_HASH = hash_customer_password("not-a-real-customer-password")


def customer_email_configuration() -> Tuple[str, str, str]:
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    sender = os.getenv("RESEND_FROM_EMAIL", "").strip()
    base_url = os.getenv("CUSTOMER_WEB_URL", "").strip()
    parts = urlsplit(base_url)
    if (
        not api_key
        or not sender
        or not base_url
        or parts.scheme not in {"https", "http"}
        or not parts.netloc
        or parts.username
        or parts.password
        or (parts.scheme != "https" and parts.hostname not in {"localhost", "127.0.0.1"})
    ):
        raise RuntimeError("Customer email delivery is not configured.")
    return api_key, sender, base_url.rstrip("/")


def send_customer_email(
    recipient: str, subject: str, action: str, token: str
) -> None:
    """Send a transactional Resend email; the secret and provider response stay private."""
    api_key, sender, base_url = customer_email_configuration()
    parts = urlsplit(base_url)
    query = dict()
    if parts.query:
        from urllib.parse import parse_qsl

        query.update(parse_qsl(parts.query, keep_blank_values=True))
    query.update({"customer_action": action, "token": token})
    action_url = urlunsplit(
        (parts.scheme, parts.netloc, parts.path or "/", urlencode(query), "")
    )
    safe_url = html.escape(action_url, quote=True)
    safe_action = html.escape(action.replace("_", " "), quote=True)
    payload = {
        "from": sender,
        "to": [recipient],
        "subject": subject,
        "text": f"Use this link to {action.replace('_', ' ')}: {action_url}\n"
        "If you did not request this, you can ignore this message.",
        "html": (
            "<p>Use the link below to "
            + safe_action
            + ":</p><p><a href=\""
            + safe_url
            + "\">Continue</a></p><p>If you did not request this, ignore this message.</p>"
        ),
    }
    response = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=8.0,
    )
    response.raise_for_status()


class CustomerAuthMixin:
    """Customer persistence methods used by DataStore."""

    def migrate_customer_schema(self) -> None:
        self.db.migrate_customer_schema()

    def customer_schema_ready(self) -> bool:
        conn = self._connect()
        try:
            if self.db.dialect == "sqlite":
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'customer_schema_migrations'"
                ).fetchone()
            else:
                exists = conn.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'customer_schema_migrations'"
                ).fetchone()
            if not exists:
                return False
            return bool(
                conn.execute(
                    "SELECT 1 FROM customer_schema_migrations WHERE migration_key = ?",
                    (CUSTOMER_MIGRATION_KEY,),
                ).fetchone()
            )
        finally:
            conn.close()

    def register_customer(
        self, email: str, display_name: str
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        clean_email = email.strip().lower()
        now = _iso(_now())
        password_hash = hash_customer_password(secrets.token_urlsafe(32))
        conn = self._connect()
        try:
            self._begin_write(conn, f"customer-register:{clean_email}")
            row = conn.execute(
                "SELECT id, email, email_verified_at FROM customer_accounts WHERE email = ?",
                (clean_email,),
            ).fetchone()
            if row:
                conn.rollback()
                return dict(row), False
            conn.execute(
                "INSERT INTO customer_accounts "
                "(email, display_name, password_hash, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (clean_email, display_name.strip(), password_hash, now, now),
            )
            account = conn.execute(
                "SELECT id, email, display_name, email_verified_at, created_at, "
                "updated_at, last_login_at FROM customer_accounts WHERE email = ?",
                (clean_email,),
            ).fetchone()
            conn.commit()
            return dict(account) if account else None, True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def customer_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM customer_accounts WHERE email = ?",
                (email.strip().lower(),),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    @staticmethod
    def _public_customer(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "email": row["email"],
            "display_name": row["display_name"],
            "email_verified": bool(row.get("email_verified_at")),
            "created_at": row["created_at"],
            "last_login_at": row.get("last_login_at"),
        }

    def create_customer_action(
        self, customer_id: int, purpose: str, lifetime: timedelta, ticket_token: str = ""
    ) -> str:
        raw_token = secrets.token_urlsafe(32)
        now = _now()
        conn = self._connect()
        try:
            self._begin_write(conn, f"customer-action:{customer_id}:{purpose}:{ticket_token}")
            conn.execute(
                "DELETE FROM customer_action_tokens WHERE token_hash IN ("
                "SELECT token_hash FROM customer_action_tokens "
                "WHERE consumed_at IS NOT NULL OR expires_at <= ? "
                "ORDER BY created_at, token_hash LIMIT ?)",
                (_iso(now), CUSTOMER_LIMIT_CLEANUP_BATCH),
            )
            if ticket_token:
                conn.execute(
                    "DELETE FROM customer_action_tokens WHERE customer_id = ? "
                    "AND purpose = ? AND ticket_token = ?",
                    (customer_id, purpose, ticket_token),
                )
            else:
                conn.execute(
                    "DELETE FROM customer_action_tokens WHERE customer_id = ? "
                    "AND purpose = ?",
                    (customer_id, purpose),
                )
            conn.execute(
                "INSERT INTO customer_action_tokens "
                "(token_hash, customer_id, purpose, ticket_token, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    token_fingerprint(raw_token),
                    customer_id,
                    purpose,
                    ticket_token or None,
                    _iso(now),
                    _iso(now + lifetime),
                ),
            )
            conn.commit()
            return raw_token
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def verify_customer_email(self, raw_token: str, new_password: str) -> bool:
        if password_problem(new_password):
            return False
        conn = self._connect()
        try:
            now = _iso(_now())
            row = conn.execute(
                "SELECT customer_id FROM customer_action_tokens "
                "WHERE token_hash = ? AND purpose = 'verify_email' "
                "AND consumed_at IS NULL AND expires_at > ?",
                (token_fingerprint(raw_token), now),
            ).fetchone()
            if not row:
                return False
            customer_id = int(row["customer_id"])
        finally:
            conn.close()
        password_hash = hash_customer_password(new_password)
        conn = self._connect()
        try:
            self._begin_write(conn, "customer-verify:" + token_fingerprint(raw_token))
            row = conn.execute(
                "SELECT customer_id FROM customer_action_tokens "
                "WHERE token_hash = ? AND purpose = 'verify_email' "
                "AND consumed_at IS NULL AND expires_at > ?",
                (token_fingerprint(raw_token), _iso(_now())),
            ).fetchone()
            if not row or int(row["customer_id"]) != customer_id:
                conn.rollback()
                return False
            now = _iso(_now())
            conn.execute(
                "UPDATE customer_action_tokens SET consumed_at = ? WHERE token_hash = ?",
                (now, token_fingerprint(raw_token)),
            )
            updated = conn.execute(
                "UPDATE customer_accounts SET password_hash = ?, email_verified_at = ?, "
                "updated_at = ? "
                "WHERE id = ? AND email_verified_at IS NULL",
                (password_hash, now, now, row["customer_id"]),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return False
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def login_customer(
        self, email: str, password: str, user_agent: str = ""
    ) -> Optional[Tuple[Dict[str, str], Dict[str, Any]]]:
        row = self.customer_by_email(email)
        stored_hash = row["password_hash"] if row else _DUMMY_PASSWORD_HASH
        valid = verify_customer_password(password, stored_hash)
        if not row or not valid or not row["email_verified_at"]:
            return None
        return self.start_customer_session(int(row["id"]), user_agent), row

    def start_customer_session(self, customer_id: int, user_agent: str = "") -> Dict[str, str]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = _now()
        expires = now + timedelta(hours=CUSTOMER_SESSION_HOURS)
        conn = self._connect()
        try:
            self._begin_write(conn, f"customer-session-rotate:{customer_id}")
            conn.execute(
                "DELETE FROM customer_sessions WHERE expires_at <= ?", (_iso(now),)
            )
            conn.execute(
                "DELETE FROM customer_sessions WHERE customer_id = ?", (customer_id,)
            )
            conn.execute(
                "INSERT INTO customer_sessions "
                "(token_hash, customer_id, created_at, expires_at, last_seen_at, "
                "user_agent, csrf_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    token_fingerprint(token),
                    customer_id,
                    _iso(now),
                    _iso(expires),
                    _iso(now),
                    (user_agent or "")[:200],
                    token_fingerprint(csrf),
                ),
            )
            conn.execute(
                "UPDATE customer_accounts SET last_login_at = ?, updated_at = ? WHERE id = ?",
                (_iso(now), _iso(now), customer_id),
            )
            conn.commit()
            return {
                "token": token,
                "csrf_token": csrf,
                "expires_at": _iso(expires),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def customer_session_user(self, token: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        fingerprint = token_fingerprint(token)
        now = _iso(_now())
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT s.token_hash, s.csrf_hash, s.expires_at, c.* "
                "FROM customer_sessions s JOIN customer_accounts c ON c.id = s.customer_id "
                "WHERE s.token_hash = ? AND s.expires_at > ? AND c.email_verified_at IS NOT NULL",
                (fingerprint, now),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE customer_sessions SET last_seen_at = ? WHERE token_hash = ?",
                (now, fingerprint),
            )
            conn.commit()
            return {
                "user": self._public_customer(dict(row)),
                "token_hash": row["token_hash"],
                "csrf_hash": row["csrf_hash"],
                "expires_at": row["expires_at"],
            }
        finally:
            conn.close()

    def end_customer_session(self, token_hash: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM customer_sessions WHERE token_hash = ?", (token_hash,)
            )
            conn.commit()
        finally:
            conn.close()

    def update_customer_profile(
        self, customer_id: int, display_name: str
    ) -> Optional[Dict[str, Any]]:
        now = _iso(_now())
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE customer_accounts SET display_name = ?, updated_at = ? WHERE id = ?",
                (display_name.strip(), now, customer_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT id, email, display_name, email_verified_at, created_at, "
                "updated_at, last_login_at FROM customer_accounts WHERE id = ?",
                (customer_id,),
            ).fetchone()
            return self._public_customer(dict(row)) if row else None
        finally:
            conn.close()

    def change_customer_password(
        self, customer_id: int, current_password: str, new_password: str
    ) -> Tuple[str, Optional[Dict[str, str]]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT password_hash FROM customer_accounts WHERE id = ?",
                (customer_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row or not verify_customer_password(current_password, row["password_hash"]):
            return "wrong", None
        problem = password_problem(new_password)
        if problem:
            return "weak", None
        old_hash = row["password_hash"]
        new_hash = hash_customer_password(new_password)
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = _now()
        expires = now + timedelta(hours=CUSTOMER_SESSION_HOURS)
        conn = self._connect()
        try:
            self._begin_write(conn, f"customer-password:{customer_id}")
            latest = conn.execute(
                "SELECT password_hash FROM customer_accounts WHERE id = ?",
                (customer_id,),
            ).fetchone()
            if not latest or not hmac.compare_digest(latest["password_hash"], old_hash):
                conn.rollback()
                return "wrong", None
            conn.execute(
                "UPDATE customer_accounts SET password_hash = ?, updated_at = ? WHERE id = ?",
                (new_hash, _iso(now), customer_id),
            )
            conn.execute(
                "UPDATE customer_action_tokens SET consumed_at = ? "
                "WHERE customer_id = ? AND purpose = 'password_reset' "
                "AND consumed_at IS NULL",
                (_iso(now), customer_id),
            )
            conn.execute(
                "DELETE FROM customer_sessions WHERE customer_id = ?", (customer_id,)
            )
            conn.execute(
                "INSERT INTO customer_sessions "
                "(token_hash, customer_id, created_at, expires_at, last_seen_at, csrf_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    token_fingerprint(token),
                    customer_id,
                    _iso(now),
                    _iso(expires),
                    _iso(now),
                    token_fingerprint(csrf),
                ),
            )
            conn.commit()
            return "ok", {"token": token, "csrf_token": csrf, "expires_at": _iso(expires)}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def reset_customer_password(self, raw_token: str, new_password: str) -> bool:
        problem = password_problem(new_password)
        if problem:
            return False
        now = _iso(_now())
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT customer_id FROM customer_action_tokens "
                "WHERE token_hash = ? AND purpose = 'password_reset' "
                "AND consumed_at IS NULL AND expires_at > ?",
                (token_fingerprint(raw_token), now),
            ).fetchone()
            if not row:
                conn.rollback()
                return False
        finally:
            conn.close()
        new_hash = hash_customer_password(new_password)
        conn = self._connect()
        try:
            customer_id = int(row["customer_id"])
            self._begin_write(conn, f"customer-password:{customer_id}")
            row = conn.execute(
                "SELECT customer_id FROM customer_action_tokens "
                "WHERE token_hash = ? AND purpose = 'password_reset' "
                "AND consumed_at IS NULL AND expires_at > ?",
                (token_fingerprint(raw_token), now),
            ).fetchone()
            if not row or int(row["customer_id"]) != customer_id:
                conn.rollback()
                return False
            conn.execute(
                "UPDATE customer_action_tokens SET consumed_at = ? WHERE token_hash = ?",
                (now, token_fingerprint(raw_token)),
            )
            conn.execute(
                "UPDATE customer_accounts SET password_hash = ?, updated_at = ? WHERE id = ?",
                (new_hash, now, row["customer_id"]),
            )
            conn.execute(
                "DELETE FROM customer_sessions WHERE customer_id = ?", (row["customer_id"],)
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def use_customer_rate_limit(
        self, bucket: str, operation: str, maximum: int, window_seconds: int = RATE_WINDOW_SECONDS
    ) -> bool:
        key_hash = token_fingerprint(bucket)
        conn = self._connect()
        try:
            self._begin_write(conn, "customer-rate:" + key_hash)
            now = _now()
            cutoff = _iso(now - timedelta(seconds=window_seconds))
            row = conn.execute(
                "SELECT window_started_at, attempts FROM customer_auth_limits "
                "WHERE bucket_hash = ?",
                (key_hash,),
            ).fetchone()
            if not row or row["window_started_at"] <= cutoff:
                if self.db.dialect == "postgres":
                    conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(?))",
                        ("customer-rate-limit-capacity",),
                    )
                now = _now()
                now_iso = _iso(now)
                cutoff = _iso(now - timedelta(seconds=window_seconds))
                row = conn.execute(
                    "SELECT window_started_at, attempts FROM customer_auth_limits "
                    "WHERE bucket_hash = ?",
                    (key_hash,),
                ).fetchone()
                if not row:
                    tracked = int(
                        conn.execute(
                            "SELECT COUNT(*) AS c FROM customer_auth_limits"
                        ).fetchone()["c"]
                    )
                    if tracked >= MAX_TRACKED_CUSTOMER_LIMITS:
                        conn.execute(
                            "DELETE FROM customer_auth_limits WHERE bucket_hash IN ("
                            "SELECT bucket_hash FROM customer_auth_limits "
                            "WHERE window_started_at <= ? "
                            "ORDER BY window_started_at, bucket_hash LIMIT ?)",
                            (cutoff, CUSTOMER_LIMIT_CLEANUP_BATCH),
                        )
                        tracked = int(
                            conn.execute(
                                "SELECT COUNT(*) AS c FROM customer_auth_limits"
                            ).fetchone()["c"]
                        )
                        if tracked >= MAX_TRACKED_CUSTOMER_LIMITS:
                            conn.commit()
                            raise CustomerRateLimitStorageFull(
                                "Customer rate-limit storage is full of active counters."
                            )
                    conn.execute(
                        "INSERT INTO customer_auth_limits "
                        "(bucket_hash, operation, window_started_at, attempts) "
                        "VALUES (?, ?, ?, 1) ON CONFLICT (bucket_hash) DO UPDATE SET "
                        "operation = excluded.operation, window_started_at = excluded.window_started_at, "
                        "attempts = 1",
                        (key_hash, operation, now_iso),
                    )
                    attempts = 1
                elif row["window_started_at"] <= cutoff:
                    conn.execute(
                        "UPDATE customer_auth_limits SET operation = ?, window_started_at = ?, "
                        "attempts = 1 WHERE bucket_hash = ?",
                        (operation, now_iso, key_hash),
                    )
                    attempts = 1
                else:
                    attempts = int(row["attempts"]) + 1
                    conn.execute(
                        "UPDATE customer_auth_limits SET attempts = ? WHERE bucket_hash = ?",
                        (attempts, key_hash),
                    )
            else:
                attempts = int(row["attempts"]) + 1
                conn.execute(
                    "UPDATE customer_auth_limits SET attempts = ? WHERE bucket_hash = ?",
                    (attempts, key_hash),
                )
            conn.commit()
            return attempts <= maximum
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def customer_ticket_for_claim(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT t.token, t.student_email, "
                "EXISTS (SELECT 1 FROM customer_ticket_ownership o "
                "WHERE o.ticket_token = t.token) AS claimed "
                "FROM tickets t WHERE lower(t.ticket_id) = lower(?)",
                (ticket_id.strip(),),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def claim_customer_ticket(self, raw_token: str) -> bool:
        now = _iso(_now())
        conn = self._connect()
        try:
            self._begin_write(conn, "customer-ticket-claim:" + token_fingerprint(raw_token))
            row = conn.execute(
                "SELECT customer_id, ticket_token FROM customer_action_tokens "
                "WHERE token_hash = ? AND purpose = 'ticket_claim' "
                "AND consumed_at IS NULL AND expires_at > ? AND ticket_token IS NOT NULL",
                (token_fingerprint(raw_token), now),
            ).fetchone()
            if not row:
                conn.rollback()
                return False
            conn.execute(
                "UPDATE customer_action_tokens SET consumed_at = ? WHERE token_hash = ?",
                (now, token_fingerprint(raw_token)),
            )
            inserted = conn.execute(
                "INSERT INTO customer_ticket_ownership (ticket_token, customer_id, claimed_at) "
                "VALUES (?, ?, ?) ON CONFLICT (ticket_token) DO NOTHING",
                (row["ticket_token"], row["customer_id"], now),
            ).rowcount
            conn.commit()
            return bool(inserted)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def customer_tickets(self, customer_id: int) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT t.token, t.ticket_id, t.event_id, t.event_title, e.date, e.time, "
                "e.venue, t.is_checked_in, t.check_in_time, t.created_at, o.claimed_at "
                "FROM customer_ticket_ownership o JOIN tickets t ON t.token = o.ticket_token "
                "JOIN events e ON e.id = t.event_id "
                "WHERE o.customer_id = ? ORDER BY COALESCE(t.created_ms, t.created_at) DESC",
                (customer_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
