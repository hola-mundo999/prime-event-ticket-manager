"""Prime IT Club event and ticket API.

Serves the public registration page and the JSON API it uses, plus the admin
endpoints the separate `prime-ticket-admin` dashboard calls. Admin routes are
guarded by the ADMIN_TOKEN environment variable and an `x-admin-token` header.

Storage is SQLite locally and Postgres (Supabase) in production, chosen by the
DATABASE_URL environment variable - see db.py.
"""

from __future__ import annotations

import csv
import io
import os
import secrets
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from models import DataStore, EventInput, EventPatch, RegistrationRequest
from qr_service import generate_qr_base64

app = FastAPI(
    title="Prime IT Club Event & Ticket API",
    description="Event registration, QR passes and gate check-in for Prime IT Club.",
    version="2.1.0",
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
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

if ASSETS_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")


def admin_guard(
    x_admin_token: Optional[str] = Header(default=None, alias="x-admin-token"),
) -> None:
    """Every admin route needs the shared admin token from the server env."""
    if not ADMIN_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Admin access is not configured. Set the ADMIN_TOKEN environment variable.",
        )
    if not x_admin_token or not secrets.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid admin token.")


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
    body = {
        "status": "ok" if storage["ok"] else "degraded",
        "service": "prime-event-ticket-manager",
        "version": "2.1.0",
        "storage": storage,
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
# Admin API
# --------------------------------------------------------------------------- #
@app.get("/api/admin/session", dependencies=[Depends(admin_guard)])
def admin_session():
    return {"ok": True, "role": "admin"}


@app.get("/api/admin/overview", dependencies=[Depends(admin_guard)])
def admin_overview():
    analytics = store.analytics()
    latest = store.attendees(limit=6)
    return {**analytics, "recent": latest["items"]}


@app.get("/api/admin/events", dependencies=[Depends(admin_guard)])
def admin_events():
    return [e.model_dump() for e in store.get_events()]


@app.post("/api/admin/events", dependencies=[Depends(admin_guard)], status_code=201)
def admin_create_event(payload: EventInput):
    return store.create_event(payload).model_dump()


@app.patch("/api/admin/events/{event_id}", dependencies=[Depends(admin_guard)])
def admin_update_event(event_id: int, patch: EventPatch):
    updated = store.update_event(event_id, patch)
    if not updated:
        raise HTTPException(status_code=404, detail="Event not found")
    return updated.model_dump()


@app.delete("/api/admin/events/{event_id}", dependencies=[Depends(admin_guard)])
def admin_delete_event(event_id: int):
    if not store.get_event(event_id):
        raise HTTPException(status_code=404, detail="Event not found")
    return store.delete_event(event_id)


@app.get("/api/admin/attendees", dependencies=[Depends(admin_guard)])
def admin_attendees(
    event_id: Optional[int] = None,
    q: str = "",
    status: str = "all",
    limit: int = 200,
    offset: int = 0,
):
    return store.attendees(event_id=event_id, query=q, status=status, limit=limit, offset=offset)


@app.get("/api/admin/export.csv", dependencies=[Depends(admin_guard)])
def admin_export_csv(event_id: Optional[int] = None):
    return export_csv(event_id=event_id)


@app.post("/api/admin/selftest", dependencies=[Depends(admin_guard)])
def admin_selftest():
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
