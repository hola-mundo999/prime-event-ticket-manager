"""Admin accounts for the Prime IT Club console.

One shared token is fine for two people and hopeless for six. This module gives
every coordinator their own account: a password hashed with PBKDF2-SHA256, a
server-side session that can be revoked, and a role that decides what they may
do.

    owner   full control, including the event catalog and who else can sign in
    staff   the door: check-in, the attendee roster and exports

Passwords are never stored, and neither are session tokens: the database holds a
SHA-256 fingerprint of the token, so a database leak cannot be replayed as a
login. Sessions expire after 12 hours and are renewed on use.

`AuthMixin` is mixed into `DataStore`, so it shares that class's connection
handling and works the same on SQLite and Postgres.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
import time
import struct
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ROUNDS = 210_000
MIN_PASSWORD_LENGTH = 10

#: How long a sign-in lasts before it has to be repeated.
SESSION_HOURS = 12

#: A session touched after this much time is extended back to SESSION_HOURS.
RENEW_AFTER_HOURS = 6

#: Failed sign-ins before an address is told to wait.
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 10 * 60
AUTH_FAILURE_CLEANUP_BATCH = 100
MAX_TRACKED_AUTH_FAILURES = 10_000
MAX_LOCAL_FAILURE_KEYS = 5_000
MFA_CHALLENGE_SECONDS = 5 * 60
MAX_MFA_FAILURES = 5
MFA_FAILURE_WINDOW_SECONDS = 10 * 60
ADMIN_EMAIL = "samyog.sapkota1@gmail.com"
TOTP_ISSUER = "Prime IT Club"

ROLES = ("owner", "staff")
ROLE_LABELS = {"owner": "Owner", "staff": "Staff"}

WEAK_PASSWORDS = {
    "password",
    "password1",
    "password123",
    "passw0rd1",
    "admin12345",
    "administrator",
    "primeitclub",
    "letmein123",
    "qwerty1234",
    "1234567890",
}


# --------------------------------------------------------------------------- #
# Password hashing
# --------------------------------------------------------------------------- #
def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def hash_password(password: str) -> str:
    """``pbkdf2_sha256$rounds$salt$hash`` — self-describing, so rounds can grow."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ROUNDS)
    return f"{PASSWORD_SCHEME}${PASSWORD_ROUNDS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check against a stored hash. Never raises."""
    try:
        scheme, rounds, salt, expected = (stored or "").split("$")
        if scheme != PASSWORD_SCHEME:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), _unb64(salt), int(rounds)
        )
        return hmac.compare_digest(digest, _unb64(expected))
    except Exception:
        return False


#: Burned when an email does not exist, so a wrong address costs the same time
#: as a wrong password and cannot be told apart by a stopwatch.
_DUMMY_HASH = hash_password("not-a-real-account-password")


def password_problem(password: str) -> Optional[str]:
    """Why this password is not acceptable, or ``None`` when it is."""
    value = password or ""
    if len(value) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    if not any(c.isalpha() for c in value):
        return "Include at least one letter."
    if not any(c.isdigit() for c in value):
        return "Include at least one number."
    if value.lower() in WEAK_PASSWORDS:
        return "That password is too common. Pick something else."
    return None


# --------------------------------------------------------------------------- #
# Session tokens
# --------------------------------------------------------------------------- #
def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def token_fingerprint(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- #
# Sign-in throttle (per process; one instance serves this API)
# --------------------------------------------------------------------------- #
_FAILURES: Dict[str, List[float]] = {}
_FAILURES_LOCK = threading.Lock()


def client_rate_limit_key(source: Optional[str]) -> str:
    """Canonicalize the direct connection peer; callers must not pass proxy headers."""
    value = (source or "").strip()
    if not value:
        return "unknown"
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        return value.lower()


def throttle_remaining(rate_limit_key: str) -> int:
    """Seconds this source must wait, or 0 when a sign-in may be attempted."""
    key = client_rate_limit_key(rate_limit_key)
    cutoff = time.time() - FAILURE_WINDOW_SECONDS
    with _FAILURES_LOCK:
        recent = [t for t in _FAILURES.get(key, []) if t > cutoff]
        if recent:
            _FAILURES[key] = recent
        else:
            _FAILURES.pop(key, None)
        if len(recent) < MAX_FAILURES:
            return 0
        return max(1, int(FAILURE_WINDOW_SECONDS - (time.time() - min(recent))))


def note_failure(rate_limit_key: str) -> None:
    key = client_rate_limit_key(rate_limit_key)
    with _FAILURES_LOCK:
        _FAILURES.setdefault(key, []).append(time.time())
        if len(_FAILURES) > MAX_LOCAL_FAILURE_KEYS:
            cutoff = time.time() - FAILURE_WINDOW_SECONDS
            stale = [
                candidate
                for candidate, attempts in _FAILURES.items()
                if not any(attempt > cutoff for attempt in attempts)
            ]
            for candidate in stale:
                _FAILURES.pop(candidate, None)
            if len(_FAILURES) > MAX_LOCAL_FAILURE_KEYS:
                remove_count = len(_FAILURES) - (MAX_LOCAL_FAILURE_KEYS - 100)
                oldest = sorted(
                    _FAILURES,
                    key=lambda candidate: max(_FAILURES[candidate]),
                )[:remove_count]
                for candidate in oldest:
                    _FAILURES.pop(candidate, None)


def clear_failures(rate_limit_key: str) -> None:
    with _FAILURES_LOCK:
        _FAILURES.pop(client_rate_limit_key(rate_limit_key), None)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class DuplicateAccount(Exception):
    """That email already has an account."""


class LastOwner(Exception):
    """The change would leave the console with nobody able to manage it."""


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #
class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class UserCreate(BaseModel):
    email: EmailStr
    name: str = Field(min_length=2, max_length=80)
    password: str = Field(min_length=1, max_length=200)
    role: str = "staff"

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("role")
    @classmethod
    def known_role(cls, value: str) -> str:
        role = (value or "staff").strip().lower()
        if role not in ROLES:
            raise ValueError(f"Role must be one of: {', '.join(ROLES)}.")
        return role

    @field_validator("password")
    @classmethod
    def strong_password(cls, value: str) -> str:
        problem = password_problem(value)
        if problem:
            raise ValueError(problem)
        return value


class UserPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=80)
    role: Optional[str] = None
    active: Optional[bool] = None
    password: Optional[str] = Field(default=None, max_length=200)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() if isinstance(value, str) else value

    @field_validator("role")
    @classmethod
    def known_role(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        role = value.strip().lower()
        if role not in ROLES:
            raise ValueError(f"Role must be one of: {', '.join(ROLES)}.")
        return role

    @field_validator("password")
    @classmethod
    def strong_password(cls, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        problem = password_problem(value)
        if problem:
            raise ValueError(problem)
        return value


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=1, max_length=200)

    @field_validator("new_password")
    @classmethod
    def strong_password(cls, value: str) -> str:
        problem = password_problem(value)
        if problem:
            raise ValueError(problem)
        return value


class TotpCode(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")


class RecoveryCode(BaseModel):
    code: str = Field(min_length=8, max_length=40)


class MFAVerify(BaseModel):
    code: str = Field(min_length=6, max_length=40)


class ProfilePreferences(BaseModel):
    theme: Literal["system", "light", "dark"] = "system"
    compact_mode: bool = False


class ProfileUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=80)
    preferences: Optional[ProfilePreferences] = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def require_profile_change(self):
        if self.name is None and self.preferences is None:
            raise ValueError("At least one profile field must be provided.")
        return self


class MFAConfigurationError(RuntimeError):
    """The server-side TOTP encryption key is missing or unusable."""


def _fernet() -> Fernet:
    key = os.getenv("ADMIN_TOTP_ENCRYPTION_KEY", "")
    if not key:
        raise MFAConfigurationError(
            "ADMIN_TOTP_ENCRYPTION_KEY must be configured before MFA can be used."
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise MFAConfigurationError(
            "ADMIN_TOTP_ENCRYPTION_KEY is invalid; generate a Fernet key."
        ) from exc


def encrypt_totp_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode("ascii")).decode("ascii")


def decrypt_totp_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("ascii")
    except InvalidToken as exc:
        raise MFAConfigurationError(
            "The TOTP encryption key cannot decrypt the stored authenticator secret."
        ) from exc


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def totp_counter(secret: str, code: str, now: Optional[float] = None) -> Optional[int]:
    """Return the matching RFC 6238 counter in a one-step clock window."""
    if not re.fullmatch(r"\d{6}", code or ""):
        return None
    try:
        padded = secret + "=" * ((8 - len(secret) % 8) % 8)
        key = base64.b32decode(padded, casefold=True)
    except (ValueError, TypeError):
        return None
    current = int((time.time() if now is None else now) // 30)
    for counter in (current - 1, current, current + 1):
        digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
        expected = f"{value % 1_000_000:06d}"
        if hmac.compare_digest(expected, code):
            return counter
    return None


def provisioning_uri(secret: str, email: str) -> str:
    from urllib.parse import quote

    label = quote(f"{TOTP_ISSUER}:{email}", safe="")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(TOTP_ISSUER)}"
        "&algorithm=SHA1&digits=6&period=30"
    )


def recovery_code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
class AuthMixin:
    """Accounts and sessions on top of ``DataStore``'s connection helpers."""

    # -- rows --------------------------------------------------------------- #
    @staticmethod
    def _public_user(row: Any) -> Dict[str, Any]:
        raw_preferences = row.get("preferences") or "{}"
        try:
            preferences = (
                json.loads(raw_preferences)
                if isinstance(raw_preferences, str)
                else raw_preferences
            )
        except (TypeError, ValueError):
            preferences = {}
        if not isinstance(preferences, dict):
            preferences = {}
        return {
            "id": int(row["id"]),
            "email": row["email"],
            "name": row["name"],
            "role": row["role"],
            "active": bool(row["active"]),
            "created_at": row["created_at"],
            "last_login_at": row["last_login_at"],
            "preferences": preferences,
        }

    def user_count(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) AS c FROM admin_users").fetchone()
            return int(row["c"]) if row else 0
        finally:
            conn.close()

    @staticmethod
    def _rate_limit_key_hash(rate_limit_key: str) -> str:
        # The legacy column is retained to avoid a schema migration.
        value = client_rate_limit_key(rate_limit_key)
        return hashlib.sha256(("source:" + value).encode("utf-8")).hexdigest()

    def _cleanup_auth_failures(self, conn: Any, cutoff: str) -> None:
        conn.execute(
            "DELETE FROM admin_auth_failures WHERE email_hash IN ("
            "SELECT email_hash FROM admin_auth_failures "
            "WHERE window_started_at <= ? "
            "ORDER BY window_started_at, email_hash LIMIT ?)",
            (cutoff, AUTH_FAILURE_CLEANUP_BATCH),
        )

    def _trim_auth_failures_to_capacity(self, conn: Any) -> None:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM admin_auth_failures"
        ).fetchone()
        tracked = int(count["c"]) if count else 0
        if tracked > MAX_TRACKED_AUTH_FAILURES:
            remove_count = min(
                AUTH_FAILURE_CLEANUP_BATCH,
                tracked - MAX_TRACKED_AUTH_FAILURES,
            )
            conn.execute(
                "DELETE FROM admin_auth_failures WHERE email_hash IN ("
                "SELECT email_hash FROM admin_auth_failures "
                "ORDER BY window_started_at, email_hash LIMIT ?)",
                (remove_count,),
            )

    def auth_throttle_remaining(self, rate_limit_key: str) -> int:
        now = _utc_now()
        key_hash = self._rate_limit_key_hash(rate_limit_key)
        conn = self._connect()
        try:
            self._begin_write(conn, "admin-auth-failures")
            self._cleanup_auth_failures(
                conn, _iso(now - timedelta(seconds=FAILURE_WINDOW_SECONDS))
            )
            self._trim_auth_failures_to_capacity(conn)
            row = conn.execute(
                "SELECT window_started_at, attempts FROM admin_auth_failures WHERE email_hash = ?",
                (key_hash,),
            ).fetchone()
            conn.commit()
        finally:
            conn.close()
        if not row or int(row["attempts"]) < MAX_FAILURES:
            return 0
        try:
            started = datetime.fromisoformat(row["window_started_at"])
        except ValueError:
            return FAILURE_WINDOW_SECONDS
        remaining = FAILURE_WINDOW_SECONDS - (_utc_now() - started).total_seconds()
        return max(1, int(remaining)) if remaining > 0 else 0

    def note_auth_failure(self, rate_limit_key: str) -> None:
        now = _utc_now()
        cutoff = _iso(now - timedelta(seconds=FAILURE_WINDOW_SECONDS))
        key_hash = self._rate_limit_key_hash(rate_limit_key)
        conn = self._connect()
        try:
            self._begin_write(conn, "admin-auth-failures")
            self._cleanup_auth_failures(conn, cutoff)
            exists = conn.execute(
                "SELECT 1 FROM admin_auth_failures WHERE email_hash = ?",
                (key_hash,),
            ).fetchone()
            if not exists:
                count = conn.execute(
                    "SELECT COUNT(*) AS c FROM admin_auth_failures"
                ).fetchone()
                tracked = int(count["c"]) if count else 0
                if tracked >= MAX_TRACKED_AUTH_FAILURES:
                    remove_count = min(
                        AUTH_FAILURE_CLEANUP_BATCH,
                        max(1, tracked - MAX_TRACKED_AUTH_FAILURES + 1),
                    )
                    conn.execute(
                        "DELETE FROM admin_auth_failures WHERE email_hash IN ("
                        "SELECT email_hash FROM admin_auth_failures "
                        "ORDER BY window_started_at, email_hash LIMIT ?)",
                        (remove_count,),
                    )
            conn.execute(
                "INSERT INTO admin_auth_failures (email_hash, window_started_at, attempts) "
                "VALUES (?, ?, 1) ON CONFLICT (email_hash) DO UPDATE SET "
                "attempts = CASE WHEN admin_auth_failures.window_started_at <= ? "
                "THEN 1 ELSE admin_auth_failures.attempts + 1 END, "
                "window_started_at = CASE WHEN admin_auth_failures.window_started_at <= ? "
                "THEN excluded.window_started_at ELSE admin_auth_failures.window_started_at END",
                (key_hash, _iso(now), cutoff, cutoff),
            )
            conn.commit()
        finally:
            conn.close()

    def clear_auth_failures(self, rate_limit_key: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM admin_auth_failures WHERE email_hash = ?",
                (self._rate_limit_key_hash(rate_limit_key),),
            )
            conn.commit()
        finally:
            conn.close()

    def _mfa_row_lock_clause(self) -> str:
        return " FOR UPDATE OF c, u" if self.db.dialect == "postgres" else ""

    def _mfa_user_lock_clause(self) -> str:
        return " FOR UPDATE" if self.db.dialect == "postgres" else ""

    @staticmethod
    def _mfa_failure_window_expired(row: Any, now: datetime) -> bool:
        started = row.get("mfa_failure_window_started_at")
        if not started:
            return True
        try:
            return datetime.fromisoformat(started) <= now - timedelta(
                seconds=MFA_FAILURE_WINDOW_SECONDS
            )
        except ValueError:
            return True

    @staticmethod
    def _mfa_failure_wait(row: Any, now: datetime) -> int:
        if int(row.get("mfa_failure_attempts") or 0) < MAX_MFA_FAILURES:
            return 0
        started = row.get("mfa_failure_window_started_at")
        if not started:
            return 0
        try:
            elapsed = (now - datetime.fromisoformat(started)).total_seconds()
        except ValueError:
            return 0
        remaining = MFA_FAILURE_WINDOW_SECONDS - elapsed
        return max(1, int(remaining)) if remaining > 0 else 0

    def mfa_throttle_remaining(self, user_id: int) -> int:
        now = _utc_now()
        conn = self._connect()
        try:
            if self.db.dialect == "sqlite":
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT mfa_failure_window_started_at, mfa_failure_attempts "
                "FROM admin_users WHERE id = ?" + self._mfa_user_lock_clause(),
                (user_id,),
            ).fetchone()
            if not row:
                conn.rollback()
                return 0
            if (
                row["mfa_failure_window_started_at"]
                and self._mfa_failure_window_expired(row, now)
            ):
                conn.execute(
                    "UPDATE admin_users SET mfa_failure_window_started_at = NULL, "
                    "mfa_failure_attempts = 0 WHERE id = ?",
                    (user_id,),
                )
                row = {
                    **row,
                    "mfa_failure_window_started_at": None,
                    "mfa_failure_attempts": 0,
                }
            remaining = self._mfa_failure_wait(row, now)
            conn.commit()
            return remaining
        finally:
            conn.close()

    def _record_mfa_failure(self, conn: Any, row: Any, challenge_hash: str) -> None:
        now = _utc_now()
        if self._mfa_failure_window_expired(row, now):
            window_started_at = _iso(now)
            attempts = 1
        else:
            window_started_at = row["mfa_failure_window_started_at"]
            attempts = int(row["mfa_failure_attempts"] or 0) + 1

        challenge = conn.execute(
            "UPDATE admin_auth_challenges SET attempts = attempts + 1 "
            "WHERE challenge_hash = ? AND attempts < ?",
            (challenge_hash, MAX_MFA_FAILURES),
        )
        user = conn.execute(
            "UPDATE admin_users SET mfa_failure_window_started_at = ?, "
            "mfa_failure_attempts = ? WHERE id = ?",
            (window_started_at, attempts, int(row["id"])),
        )
        if challenge.rowcount != 1 or user.rowcount != 1:
            raise RuntimeError("Could not record the MFA verification failure.")

    @staticmethod
    def _reset_mfa_failures(conn: Any, user_id: int) -> None:
        conn.execute(
            "UPDATE admin_users SET mfa_failure_window_started_at = NULL, "
            "mfa_failure_attempts = 0 WHERE id = ?",
            (user_id,),
        )

    def active_owner_count(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM admin_users WHERE role = 'owner' AND active = TRUE"
            ).fetchone()
            return int(row["c"]) if row else 0
        finally:
            conn.close()

    def find_user(self, email: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM admin_users WHERE email = ?", ((email or "").strip().lower(),)
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def list_users(self) -> List[Dict[str, Any]]:
        """Every account, newest first, with how many sessions it has open."""
        now = _iso(_utc_now())
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT u.*, (SELECT COUNT(*) FROM admin_sessions s "
                "  WHERE s.user_id = u.id AND s.expires_at > ?) AS open_sessions "
                "FROM admin_users u ORDER BY u.created_at ASC, u.id ASC",
                (now,),
            ).fetchall()
        finally:
            conn.close()
        return [
            {**self._public_user(row), "open_sessions": int(row.get("open_sessions") or 0)}
            for row in rows
        ]

    # -- accounts ----------------------------------------------------------- #
    def create_user(
        self, email: str, name: str, password: str, role: str = "staff"
    ) -> Dict[str, Any]:
        clean_email = (email or "").strip().lower()
        clean_name = (name or "").strip() or clean_email.split("@")[0]
        clean_role = (role or "staff").strip().lower()
        if clean_email != ADMIN_EMAIL:
            raise ValueError(f"Only {ADMIN_EMAIL} may have an admin account.")
        if clean_role != "owner":
            raise ValueError("The authorized administrator must have the owner role.")
        if clean_role not in ROLES:
            raise ValueError(f"Role must be one of: {', '.join(ROLES)}.")
        problem = password_problem(password)
        if problem:
            raise ValueError(problem)
        if "@" not in clean_email:
            raise ValueError("A valid email address is required.")

        created_at = _iso(_utc_now())
        conn = self._connect()
        try:
            sql = (
                "INSERT INTO admin_users "
                "(email, name, password_hash, role, active, created_at) "
                "VALUES (?, ?, ?, ?, TRUE, ?)"
            )
            params = (clean_email, clean_name, hash_password(password), clean_role, created_at)
            if self.db.dialect == "postgres":
                row = conn.execute(sql + " RETURNING id", params).fetchone()
                user_id = int(row["id"])
            else:
                user_id = int(conn.execute(sql, params).lastrowid or 0)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            if self._is_integrity_error(exc):
                raise DuplicateAccount(clean_email) from exc
            raise
        finally:
            conn.close()
        return {
            "id": user_id,
            "email": clean_email,
            "name": clean_name,
            "role": clean_role,
            "active": True,
            "created_at": created_at,
            "last_login_at": None,
            "preferences": {},
        }

    def update_user(
        self, user_id: int, changes: Dict[str, Any], keep_session: str = ""
    ) -> Optional[Dict[str, Any]]:
        """Rename, re-role, deactivate or re-password an account.

        Refuses to demote or deactivate the last active owner, which would lock
        everyone out of the console.
        """
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM admin_users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                return None
            if row["email"].strip().lower() != ADMIN_EMAIL:
                raise ValueError("This administrator account is not authorized.")
            if "role" in changes or "active" in changes:
                raise ValueError("The sole authorized administrator cannot be managed.")

            sets: List[str] = []
            params: List[Any] = []
            if changes.get("name"):
                sets.append("name = ?")
                params.append(str(changes["name"]).strip())
            if "preferences" in changes:
                preferences = changes["preferences"]
                if not isinstance(preferences, dict):
                    raise ValueError("Profile preferences must be an object.")
                sets.append("preferences = ?")
                params.append(json.dumps(preferences, sort_keys=True, separators=(",", ":")))
            if changes.get("role"):
                sets.append("role = ?")
                params.append(str(changes["role"]).strip().lower())
            if changes.get("active") is not None:
                sets.append("active = ?")
                params.append(bool(changes["active"]))
            if changes.get("password"):
                problem = password_problem(changes["password"])
                if problem:
                    raise ValueError(problem)
                sets.append("password_hash = ?")
                params.append(hash_password(changes["password"]))

            if not sets:
                return self._public_user(row)

            params.append(user_id)
            conn.execute(f"UPDATE admin_users SET {', '.join(sets)} WHERE id = ?", params)
            if changes.get("password"):
                # Old sessions must not survive a password reset.
                if keep_session:
                    conn.execute(
                        "DELETE FROM admin_sessions WHERE user_id = ? AND token_hash <> ?",
                        (user_id, keep_session),
                    )
                else:
                    conn.execute("DELETE FROM admin_sessions WHERE user_id = ?", (user_id,))
            if changes.get("active") is False:
                conn.execute("DELETE FROM admin_sessions WHERE user_id = ?", (user_id,))
            conn.commit()
            fresh = conn.execute("SELECT * FROM admin_users WHERE id = ?", (user_id,)).fetchone()
        finally:
            conn.close()
        return self._public_user(fresh) if fresh else None

    def change_password(
        self, user_id: int, current_password: str, new_password: str, keep_session: str = ""
    ) -> str:
        """Self-service password change. Returns ``ok``, ``wrong`` or ``weak``."""
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM admin_users WHERE id = ?", (user_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            return "wrong"
        if not verify_password(current_password, row["password_hash"]):
            return "wrong"
        problem = password_problem(new_password)
        if problem:
            return "weak"
        self.update_user(user_id, {"password": new_password}, keep_session=keep_session)
        return "ok"

    # -- sessions ----------------------------------------------------------- #
    def start_session(self, user_id: int, user_agent: str = "") -> Dict[str, Any]:
        token = new_session_token()
        csrf_token = secrets.token_urlsafe(32)
        now = _utc_now()
        expires = now + timedelta(hours=SESSION_HOURS)
        conn = self._connect()
        try:
            # Housekeeping: an expired session is useless, so drop it now.
            conn.execute(
                "DELETE FROM admin_sessions WHERE expires_at <= ?", (_iso(now),)
            )
            conn.execute(
                "INSERT INTO admin_sessions "
                "(token_hash, user_id, created_at, expires_at, last_seen_at, user_agent, csrf_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    token_fingerprint(token),
                    user_id,
                    _iso(now),
                    _iso(expires),
                    _iso(now),
                    (user_agent or "")[:200],
                    token_fingerprint(csrf_token),
                ),
            )
            conn.execute(
                "UPDATE admin_users SET last_login_at = ? WHERE id = ?", (_iso(now), user_id)
            )
            conn.commit()
        finally:
            conn.close()
        return {"token": token, "csrf_token": csrf_token, "expires_at": _iso(expires)}

    def session_user(self, token: str) -> Optional[Dict[str, Any]]:
        """Resolve a bearer token, renewing it when it is getting old."""
        if not token:
            return None
        fingerprint = token_fingerprint(token)
        now = _utc_now()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT s.token_hash, s.csrf_hash, s.expires_at, s.last_seen_at, u.* "
                "FROM admin_sessions s JOIN admin_users u ON u.id = s.user_id "
                "WHERE s.token_hash = ?",
                (fingerprint,),
            ).fetchone()
            if not row:
                return None
            if (
                row["expires_at"] <= _iso(now)
                or not bool(row["active"])
                or not bool(row["mfa_enabled"])
                or row["email"].strip().lower() != ADMIN_EMAIL
            ):
                conn.execute(
                    "DELETE FROM admin_sessions WHERE token_hash = ?", (fingerprint,)
                )
                conn.commit()
                return None

            expires_at = row["expires_at"]
            try:
                expires = datetime.fromisoformat(expires_at)
            except ValueError:
                expires = now
            # Only write when the session is actually getting stale, so a busy
            # console does not write on every single request.
            if expires - now < timedelta(hours=RENEW_AFTER_HOURS):
                expires = now + timedelta(hours=SESSION_HOURS)
                conn.execute(
                    "UPDATE admin_sessions SET expires_at = ?, last_seen_at = ? "
                    "WHERE token_hash = ?",
                    (_iso(expires), _iso(now), fingerprint),
                )
            else:
                conn.execute(
                    "UPDATE admin_sessions SET last_seen_at = ? WHERE token_hash = ?",
                    (_iso(now), fingerprint),
                )
            conn.commit()
            user = self._public_user(row)
        finally:
            conn.close()
        return {
            "user": user,
            "expires_at": _iso(expires),
            "token_hash": fingerprint,
            "csrf_hash": row["csrf_hash"],
        }

    def end_session(self, token_fingerprint_value: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM admin_sessions WHERE token_hash = ?",
                (token_fingerprint_value,),
            )
            conn.commit()
        finally:
            conn.close()

    def revoke_user_sessions(self, user_id: int) -> int:
        conn = self._connect()
        try:
            removed = conn.execute(
                "DELETE FROM admin_sessions WHERE user_id = ?", (user_id,)
            ).rowcount
            conn.commit()
            return removed
        finally:
            conn.close()

    def start_mfa_challenge(self, user_id: int) -> str:
        challenge = new_session_token()
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute("DELETE FROM admin_auth_challenges WHERE expires_at <= ?", (_iso(now),))
            conn.execute(
                "INSERT INTO admin_auth_challenges "
                "(challenge_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (
                    token_fingerprint(challenge),
                    user_id,
                    _iso(now),
                    _iso(now + timedelta(seconds=MFA_CHALLENGE_SECONDS)),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return challenge

    def mfa_challenge(self, challenge: str) -> Optional[Dict[str, Any]]:
        if not challenge:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT c.challenge_hash, c.expires_at, c.attempts, u.* "
                "FROM admin_auth_challenges c JOIN admin_users u ON u.id = c.user_id "
                "WHERE c.challenge_hash = ?",
                (token_fingerprint(challenge),),
            ).fetchone()
        finally:
            conn.close()
        if (
            not row
            or row["expires_at"] <= _iso(_utc_now())
            or not bool(row["active"])
            or row["email"].strip().lower() != ADMIN_EMAIL
        ):
            return None
        return dict(row)

    def record_mfa_failure(self, challenge_hash: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE admin_auth_challenges SET attempts = attempts + 1 "
                "WHERE challenge_hash = ?",
                (challenge_hash,),
            )
            conn.commit()
        finally:
            conn.close()

    def begin_totp_enrollment(self, challenge: str) -> Dict[str, str]:
        row = self.mfa_challenge(challenge)
        if not row or bool(row["mfa_enabled"]):
            raise ValueError("That sign-in challenge is no longer valid.")
        secret = new_totp_secret()
        encrypted = encrypt_totp_secret(secret)
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE admin_users SET pending_totp_secret = ? "
                "WHERE id = ? AND mfa_enabled = FALSE",
                (encrypted, int(row["id"])),
            )
            conn.commit()
        finally:
            conn.close()
        return {"secret": secret, "provisioning_uri": provisioning_uri(secret, row["email"])}

    def _insert_session(
        self, conn: Any, user_id: int, user_agent: str = ""
    ) -> Dict[str, str]:
        token = new_session_token()
        csrf_token = secrets.token_urlsafe(32)
        now = _utc_now()
        expires = now + timedelta(hours=SESSION_HOURS)
        conn.execute(
            "INSERT INTO admin_sessions "
            "(token_hash, user_id, created_at, expires_at, last_seen_at, user_agent, csrf_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                token_fingerprint(token),
                user_id,
                _iso(now),
                _iso(expires),
                _iso(now),
                (user_agent or "")[:200],
                token_fingerprint(csrf_token),
            ),
        )
        conn.execute(
            "UPDATE admin_users SET last_login_at = ? WHERE id = ?", (_iso(now), user_id)
        )
        return {"token": token, "csrf_token": csrf_token, "expires_at": _iso(expires)}

    def activate_totp(
        self,
        challenge: str,
        code: str,
        user_agent: str = "",
        rate_limit_key: str = "unknown",
    ) -> Dict[str, Any]:
        conn = self._connect()
        try:
            if self.db.dialect == "sqlite":
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT c.challenge_hash, c.expires_at, c.attempts, u.* "
                "FROM admin_auth_challenges c JOIN admin_users u ON u.id = c.user_id "
                "WHERE c.challenge_hash = ?" + self._mfa_row_lock_clause(),
                (token_fingerprint(challenge),),
            ).fetchone()
            if (
                not row
                or row["expires_at"] <= _iso(_utc_now())
                or int(row["attempts"]) >= MAX_MFA_FAILURES
                or not bool(row["active"])
                or row["email"].strip().lower() != ADMIN_EMAIL
                or bool(row["mfa_enabled"])
                or not row["pending_totp_secret"]
            ):
                conn.rollback()
                return {"status": "invalid"}
            retry_after = self._mfa_failure_wait(row, _utc_now())
            if retry_after:
                conn.rollback()
                return {"status": "throttled", "retry_after": retry_after}

            secret = decrypt_totp_secret(row["pending_totp_secret"])
            counter = totp_counter(secret, code)
            if counter is None:
                self._record_mfa_failure(conn, row, row["challenge_hash"])
                conn.commit()
                return {"status": "invalid", "email": row["email"]}

            recovery_codes = [secrets.token_urlsafe(16) for _ in range(10)]
            conn.execute(
                "UPDATE admin_users SET mfa_enabled = TRUE, totp_secret = pending_totp_secret, "
                "pending_totp_secret = NULL, totp_last_counter = ? "
                "WHERE id = ? AND mfa_enabled = FALSE",
                (counter, int(row["id"])),
            )
            for recovery_code in recovery_codes:
                conn.execute(
                    "INSERT INTO admin_recovery_codes (user_id, code_hash, created_at) "
                    "VALUES (?, ?, ?)",
                    (int(row["id"]), recovery_code_hash(recovery_code), _iso(_utc_now())),
                )
            conn.execute(
                "DELETE FROM admin_auth_challenges WHERE challenge_hash = ?",
                (row["challenge_hash"],),
            )
            self._reset_mfa_failures(conn, int(row["id"]))
            session = self._insert_session(conn, int(row["id"]), user_agent)
            conn.commit()
            clear_failures(rate_limit_key)
            self.clear_auth_failures(rate_limit_key)
            return {
                "status": "ok",
                "session": session,
                "user": self._public_user(row),
                "recovery_codes": recovery_codes,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def complete_mfa(
        self,
        challenge: str,
        code: str,
        user_agent: str = "",
        recovery: bool = False,
        rate_limit_key: str = "unknown",
    ) -> Dict[str, Any]:
        conn = self._connect()
        try:
            if self.db.dialect == "sqlite":
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT c.challenge_hash, c.expires_at, c.attempts, u.* "
                "FROM admin_auth_challenges c JOIN admin_users u ON u.id = c.user_id "
                "WHERE c.challenge_hash = ?" + self._mfa_row_lock_clause(),
                (token_fingerprint(challenge),),
            ).fetchone()
            if (
                not row
                or row["expires_at"] <= _iso(_utc_now())
                or int(row["attempts"]) >= MAX_MFA_FAILURES
                or not bool(row["active"])
                or row["email"].strip().lower() != ADMIN_EMAIL
                or not bool(row["mfa_enabled"])
                or not row["totp_secret"]
            ):
                conn.rollback()
                return {"status": "invalid"}
            retry_after = self._mfa_failure_wait(row, _utc_now())
            if retry_after:
                conn.rollback()
                return {"status": "throttled", "retry_after": retry_after}

            if recovery:
                result = conn.execute(
                    "DELETE FROM admin_recovery_codes WHERE user_id = ? AND code_hash = ?",
                    (int(row["id"]), recovery_code_hash(code)),
                )
                valid = result.rowcount == 1
                counter = None
            else:
                counter = totp_counter(decrypt_totp_secret(row["totp_secret"]), code)
                valid = counter is not None
                if valid:
                    result = conn.execute(
                        "UPDATE admin_users SET totp_last_counter = ? "
                        "WHERE id = ? AND totp_last_counter < ?",
                        (counter, int(row["id"]), counter),
                    )
                    valid = result.rowcount == 1

            if not valid:
                self._record_mfa_failure(conn, row, row["challenge_hash"])
                conn.commit()
                return {"status": "invalid", "email": row["email"]}

            conn.execute(
                "DELETE FROM admin_auth_challenges WHERE challenge_hash = ?",
                (row["challenge_hash"],),
            )
            self._reset_mfa_failures(conn, int(row["id"]))
            session = self._insert_session(conn, int(row["id"]), user_agent)
            conn.commit()
            clear_failures(rate_limit_key)
            self.clear_auth_failures(rate_limit_key)
            return {"status": "ok", "session": session, "user": self._public_user(row)}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- sign in ------------------------------------------------------------ #
    def login(
        self,
        email: str,
        password: str,
        user_agent: str = "",
        rate_limit_key: str = "unknown",
    ) -> Dict[str, Any]:
        """Check the primary factor and issue a short-lived MFA challenge.

        Returns a ``status`` the API turns into a response: ``ok``, ``unknown``,
        ``wrong``, ``inactive`` or ``throttled``. Unknown and wrong are separate
        internally but the API answers both with the same message.
        """
        clean_email = (email or "").strip().lower()
        key = client_rate_limit_key(rate_limit_key)
        wait = max(throttle_remaining(key), self.auth_throttle_remaining(key))
        if wait:
            return {"status": "throttled", "retry_after": wait}

        row = self.find_user(clean_email)
        stored = row["password_hash"] if row else _DUMMY_HASH
        matches = verify_password(password, stored)
        if not row or not matches or clean_email != ADMIN_EMAIL:
            note_failure(key)
            self.note_auth_failure(key)
            return {"status": "unknown" if not row else "wrong"}
        if not bool(row["active"]):
            return {"status": "inactive"}

        challenge = self.start_mfa_challenge(int(row["id"]))
        return {
            "status": "mfa_required",
            "challenge": challenge,
            "enrollment_required": not bool(row["mfa_enabled"]),
            "user": self._public_user(row),
        }
