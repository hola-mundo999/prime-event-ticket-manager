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
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

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


def throttle_remaining(email: str) -> int:
    """Seconds the caller must wait, or 0 when a sign-in may be attempted."""
    key = (email or "").strip().lower()
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


def note_failure(email: str) -> None:
    key = (email or "").strip().lower()
    with _FAILURES_LOCK:
        _FAILURES.setdefault(key, []).append(time.time())
        if len(_FAILURES) > 5000:  # never let the map grow without bound
            _FAILURES.clear()


def clear_failures(email: str) -> None:
    with _FAILURES_LOCK:
        _FAILURES.pop((email or "").strip().lower(), None)


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


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
class AuthMixin:
    """Accounts and sessions on top of ``DataStore``'s connection helpers."""

    # -- rows --------------------------------------------------------------- #
    @staticmethod
    def _public_user(row: Any) -> Dict[str, Any]:
        return {
            "id": int(row["id"]),
            "email": row["email"],
            "name": row["name"],
            "role": row["role"],
            "active": bool(row["active"]),
            "created_at": row["created_at"],
            "last_login_at": row["last_login_at"],
        }

    def user_count(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) AS c FROM admin_users").fetchone()
            return int(row["c"]) if row else 0
        finally:
            conn.close()

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

            sets: List[str] = []
            params: List[Any] = []
            if changes.get("name"):
                sets.append("name = ?")
                params.append(str(changes["name"]).strip())
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

            loses_owner = (
                row["role"] == "owner"
                and bool(row["active"])
                and (changes.get("role") == "staff" or changes.get("active") is False)
            )
            if loses_owner:
                owners = conn.execute(
                    "SELECT COUNT(*) AS c FROM admin_users "
                    "WHERE role = 'owner' AND active = TRUE AND id <> ?",
                    (user_id,),
                ).fetchone()
                if not owners or int(owners["c"]) == 0:
                    raise LastOwner()

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
                "(token_hash, user_id, created_at, expires_at, last_seen_at, user_agent) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    token_fingerprint(token),
                    user_id,
                    _iso(now),
                    _iso(expires),
                    _iso(now),
                    (user_agent or "")[:200],
                ),
            )
            conn.execute(
                "UPDATE admin_users SET last_login_at = ? WHERE id = ?", (_iso(now), user_id)
            )
            conn.commit()
        finally:
            conn.close()
        return {"token": token, "expires_at": _iso(expires)}

    def session_user(self, token: str) -> Optional[Dict[str, Any]]:
        """Resolve a bearer token, renewing it when it is getting old."""
        if not token:
            return None
        fingerprint = token_fingerprint(token)
        now = _utc_now()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT s.token_hash, s.expires_at, s.last_seen_at, u.* "
                "FROM admin_sessions s JOIN admin_users u ON u.id = s.user_id "
                "WHERE s.token_hash = ?",
                (fingerprint,),
            ).fetchone()
            if not row:
                return None
            if row["expires_at"] <= _iso(now) or not bool(row["active"]):
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
            user = {
                "id": int(row["id"]),
                "email": row["email"],
                "name": row["name"],
                "role": row["role"],
                "active": bool(row["active"]),
                "created_at": row["created_at"],
                "last_login_at": row["last_login_at"],
            }
        finally:
            conn.close()
        return {"user": user, "expires_at": _iso(expires), "token_hash": fingerprint}

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

    # -- sign in ------------------------------------------------------------ #
    def login(self, email: str, password: str, user_agent: str = "") -> Dict[str, Any]:
        """Check credentials and open a session.

        Returns a ``status`` the API turns into a response: ``ok``, ``unknown``,
        ``wrong``, ``inactive`` or ``throttled``. Unknown and wrong are separate
        internally but the API answers both with the same message.
        """
        clean_email = (email or "").strip().lower()
        wait = throttle_remaining(clean_email)
        if wait:
            return {"status": "throttled", "retry_after": wait}

        row = self.find_user(clean_email)
        stored = row["password_hash"] if row else _DUMMY_HASH
        matches = verify_password(password, stored)
        if not row or not matches:
            note_failure(clean_email)
            return {"status": "unknown" if not row else "wrong"}
        if not bool(row["active"]):
            return {"status": "inactive"}

        clear_failures(clean_email)
        session = self.start_session(int(row["id"]), user_agent=user_agent)
        return {
            "status": "ok",
            "token": session["token"],
            "expires_at": session["expires_at"],
            "user": self._public_user(row),
        }
