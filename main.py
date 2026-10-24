"""Prime IT Club event and ticket API.

Serves the public registration page and the JSON API it uses, plus the admin
endpoints the separate `prime-ticket-admin` dashboard calls. Each coordinator
signs in with their own account and carries a bearer session token; see auth.py.

Storage is SQLite locally and Postgres (Supabase) in production, chosen by the
DATABASE_URL environment variable - see db.py.
"""

from __future__ import annotations

import csv
import io
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from auth import (
    DuplicateAccount,
    LastOwner,
    LoginRequest,
    PasswordChange,
    UserCreate,
    UserPatch,
)
from models import DataStore, EventInput, EventPatch, RegistrationRequest
from qr_service import generate_qr_base64


def bootstrap_admin() -> Optional[str]:
    """Create the first owner from ADMIN_EMAIL / ADMIN_PASSWORD, once.

    The console has no default password. Set those two variables on the API
    host, restart, sign in, and then manage everyone else from the console.
    """
    email = os.getenv("ADMIN_EMAIL", "").strip()
    password = os.getenv("ADMIN_PASSWORD", "")
    if not email or not password:
        return None
    try:
        if store.user_count() > 0:
            return None
        user = store.create_user(
            email, os.getenv("ADMIN_NAME", "Prime IT Club"), password, role="owner"
        )
        return f"Created the first owner account for {user['email']}."
    except Exception as exc:  # weak password, database down, duplicate race
        return f"Could not create the bootstrap account: {exc}"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    message = bootstrap_admin()
    if message:
        print(f"[auth] {message}")
    yield


app = FastAPI(
    title="Prime IT Club Event & Ticket API",
    description="Event registration, QR passes and gate check-in for Prime IT Club.",
    version="2.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
)

store = DataStore()
BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"
ASSETS_DIR = BASE_DIR / "assets"

if ASSETS_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")


def _unauthorised(message: str) -> HTTPException:
    return HTTPException(
        status_code=401, detail=message, headers={"WWW-Authenticate": "Bearer"}
    )


def current_admin(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    """Resolve `Authorization: Bearer <session token>` into the signed-in user."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorised("Sign in to continue.")
    session = store.session_user(authorization.split(" ", 1)[1].strip())
    if not session:
        raise _unauthorised("That session has expired. Sign in again.")
    return session


def owner_only(session: Dict[str, Any] = Depends(current_admin)) -> Dict[str, Any]:
    """Event editing and staff management are owner-only."""
    if session["user"]["role"] != "owner":
        raise HTTPException(
            status_code=403, detail="Only an owner can do that."
        )
    return session


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    """Liveness plus the storage the API is really using.

    `storage.backend` tells you whether rows are durable (postgres) or sitting
    in a SQLite file that a redeploy will wipe (sqlite).
    """
    storage = store.health()
    try:
        accounts = store.user_count()
    except Exception:  # an older database that has no admin tables yet
        accounts = 0
    body = {
        "status": "ok" if storage["ok"] else "degraded",
        "service": "prime-event-ticket-manager",
        "version": "2.2.0",
        "storage": storage,
        "auth": {
            "configured": accounts > 0,
            "accounts": accounts,
            "ready": accounts > 0,
        },
    }
    return JSONResponse(body, status_code=200 if storage["ok"] else 503)


@app.get("/api/events")
def list_events(scope: str = "all", category: str = ""):
    events = store.get_events(include_past=(scope != "upcoming"))
    if category and category.lower() != "all":
        events = [e for e in events if e.category.lower() == category.lower()]
    return [e.model_dump() for e in events]


@app.post("/api/register")
def register_student(req: RegistrationRequest):
    ticket, created = store.register_ticket(req)
    if ticket is None:
        event = store.get_event(req.event_id)
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        if event.seats_left <= 0:
            raise HTTPException(status_code=409, detail="This event is at full capacity")
        raise HTTPException(status_code=409, detail="Duplicate registration for this event")
    return {
        "success": True,
        "duplicate": not created,
        "message": (
            "Registration confirmed."
            if created
            else "You were already registered, so here is the same pass again."
        ),
        "ticket": ticket.model_dump(),
        "qr_image": generate_qr_base64(ticket),
    }


@app.post("/api/check-in")
def check_in_ticket(token: str = ""):
    result = store.check_in(token)
    if not result["success"]:
        status = 409 if result.get("already_checked") else 404
        raise HTTPException(status_code=status, detail=result["message"])
    return result


@app.get("/api/stats")
def event_stats():
    return store.get_stats()


@app.get("/api/export.csv")
def export_csv(event_id: Optional[int] = None):
    tickets = store.all_tickets(event_id)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "ticket_id",
            "token",
            "event_id",
            "event_title",
            "student_name",
            "student_email",
            "college_id",
            "is_checked_in",
            "check_in_time",
            "created_at",
        ]
    )
    for t in tickets:
        writer.writerow(
            [
                t.ticket_id,
                t.token,
                t.event_id,
                t.event_title,
                t.student_name,
                t.student_email,
                t.college_id,
                t.is_checked_in,
                t.check_in_time or "",
                t.created_at,
            ]
        )
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="prime_event_tickets.csv"'},
    )


# --------------------------------------------------------------------------- #
# Admin sign-in
# --------------------------------------------------------------------------- #
@app.post("/api/admin/login")
def admin_login(payload: LoginRequest, user_agent: Optional[str] = Header(default=None)):
    """Exchange an email and password for a session token."""
    if store.user_count() == 0:
        # A cold deploy may have started before the database was reachable.
        bootstrap_admin()
    if store.user_count() == 0:
        raise HTTPException(
            status_code=503,
            detail=(
                "No admin account exists yet. Set ADMIN_EMAIL and ADMIN_PASSWORD on the "
                "API host (each at least 10 characters, with a number) and restart it."
            ),
        )

    result = store.login(payload.email, payload.password, user_agent=user_agent or "")
    status = result["status"]
    if status == "ok":
        return {
            "ok": True,
            "token": result["token"],
            "expires_at": result["expires_at"],
            "user": result["user"],
        }
    if status == "throttled":
        wait = int(result.get("retry_after") or 60)
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {wait} seconds.",
            headers={"Retry-After": str(wait)},
        )
    if status == "inactive":
        raise HTTPException(
            status_code=403, detail="This account has been deactivated. Ask an owner to restore it."
        )
    # Unknown address and wrong password answer identically on purpose.
    raise _unauthorised("That email and password do not match an account.")


@app.post("/api/admin/logout")
def admin_logout(session: Dict[str, Any] = Depends(current_admin)):
    store.end_session(session["token_hash"])
    return {"ok": True}


@app.get("/api/admin/session")
def admin_session(session: Dict[str, Any] = Depends(current_admin)):
    return {"ok": True, "user": session["user"], "expires_at": session["expires_at"]}


@app.post("/api/admin/password")
def admin_change_password(
    payload: PasswordChange, session: Dict[str, Any] = Depends(current_admin)
):
    """Change your own password; every other session of yours is revoked."""
    result = store.change_password(
        session["user"]["id"],
        payload.current_password,
        payload.new_password,
        keep_session=session["token_hash"],
    )
    if result == "wrong":
        raise HTTPException(status_code=400, detail="Your current password is not correct.")
    if result == "weak":
        raise HTTPException(status_code=400, detail="That new password is not strong enough.")
    return {"ok": True, "message": "Password updated. Other sessions were signed out."}


# --------------------------------------------------------------------------- #
# Admin team
# --------------------------------------------------------------------------- #
@app.get("/api/admin/users")
def admin_list_users(session: Dict[str, Any] = Depends(owner_only)):
    return {"items": store.list_users(), "roles": ["owner", "staff"]}


@app.post("/api/admin/users", status_code=201)
def admin_create_user(payload: UserCreate, session: Dict[str, Any] = Depends(owner_only)):
    try:
        return store.create_user(payload.email, payload.name, payload.password, payload.role)
    except DuplicateAccount:
        raise HTTPException(
            status_code=409, detail="An account with that email already exists."
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(
    user_id: int, patch: UserPatch, session: Dict[str, Any] = Depends(owner_only)
):
    changes = patch.model_dump(exclude_unset=True, exclude_none=True)
    if patch.active is not None:
        changes["active"] = patch.active
    try:
        updated = store.update_user(user_id, changes)
    except LastOwner:
        raise HTTPException(
            status_code=400,
            detail="This is the last active owner. Promote someone else first.",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if not updated:
        raise HTTPException(status_code=404, detail="That account does not exist.")
    return updated


# --------------------------------------------------------------------------- #
# Admin API
# --------------------------------------------------------------------------- #
@app.get("/api/admin/overview")
def admin_overview(session: Dict[str, Any] = Depends(current_admin)):
    analytics = store.analytics()
    latest = store.attendees(limit=6)
    return {**analytics, "recent": latest["items"]}


@app.get("/api/admin/events")
def admin_events(session: Dict[str, Any] = Depends(current_admin)):
    return [e.model_dump() for e in store.get_events()]


@app.post("/api/admin/events", status_code=201)
def admin_create_event(payload: EventInput, session: Dict[str, Any] = Depends(owner_only)):
    return store.create_event(payload).model_dump()


@app.patch("/api/admin/events/{event_id}")
def admin_update_event(
    event_id: int, patch: EventPatch, session: Dict[str, Any] = Depends(owner_only)
):
    updated = store.update_event(event_id, patch)
    if not updated:
        raise HTTPException(status_code=404, detail="Event not found")
    return updated.model_dump()


@app.delete("/api/admin/events/{event_id}")
def admin_delete_event(event_id: int, session: Dict[str, Any] = Depends(owner_only)):
    if not store.get_event(event_id):
        raise HTTPException(status_code=404, detail="Event not found")
    return store.delete_event(event_id)


@app.get("/api/admin/attendees")
def admin_attendees(
    event_id: Optional[int] = None,
    q: str = "",
    status: str = "all",
    limit: int = 200,
    offset: int = 0,
    session: Dict[str, Any] = Depends(current_admin),
):
    return store.attendees(event_id=event_id, query=q, status=status, limit=limit, offset=offset)


@app.get("/api/admin/export.csv")
def admin_export_csv(
    event_id: Optional[int] = None, session: Dict[str, Any] = Depends(current_admin)
):
    return export_csv(event_id=event_id)


@app.post("/api/admin/selftest")
def admin_selftest(session: Dict[str, Any] = Depends(owner_only)):
    """Register, check in and clean up a throwaway event against live storage."""
    return store.selftest()


# --------------------------------------------------------------------------- #
# Public page
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index_page():
    if INDEX_FILE.exists():
        return INDEX_FILE.read_text(encoding="utf-8")
    return "<h1>Prime IT Club ticketing</h1><p>index.html is missing.</p>"
