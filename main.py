"""Prime IT Club event and ticket API.

Serves the public registration page and the JSON API it uses, plus the admin
endpoints the separate `prime-ticket-admin` dashboard calls. Each coordinator
signs in with their own account and carries a bearer session token; see auth.py.

Storage is SQLite locally and Postgres (Supabase) in production, chosen by the
DATABASE_URL environment variable - see db.py.
"""

from __future__ import annotations

import csv
import hmac
import io
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from auth import (
    ADMIN_EMAIL,
    DuplicateAccount,
    LastOwner,
    LoginRequest,
    MFAConfigurationError,
    MFAVerify,
    MAX_MFA_FAILURES,
    MFA_CHALLENGE_SECONDS,
    PasswordChange,
    ProfileUpdate,
    TotpCode,
    UserCreate,
    UserPatch,
    client_rate_limit_key,
    note_failure,
    password_problem,
    throttle_remaining,
    token_fingerprint,
)
from customer_auth import (
    CustomerEmailRequest,
    CustomerEmailVerification,
    CustomerLogin,
    CustomerPasswordChange,
    CustomerPasswordReset,
    CustomerProfilePatch,
    CustomerRateLimitStorageFull,
    CustomerRegister,
    CustomerTicketClaim,
    CustomerTokenRequest,
    EMAIL_VERIFY_HOURS,
    MAX_ACTION_ATTEMPTS,
    MAX_LOGIN_ATTEMPTS,
    PASSWORD_RESET_MINUTES,
    TICKET_CLAIM_MINUTES,
    customer_email_configuration,
    send_customer_email,
)
from models import DataStore, EventInput, EventPatch, RegistrationRequest
from qr_service import generate_qr_base64, generate_qr_data_uri


def bootstrap_admin() -> Optional[str]:
    """Create the first owner from ADMIN_EMAIL / ADMIN_PASSWORD, once.

    The console has no default password. Set those two variables on the API
    host, restart, sign in, and then manage everyone else from the console.
    """
    email = os.getenv("ADMIN_EMAIL", "").strip()
    password = os.getenv("ADMIN_PASSWORD", "")
    if not email or not password:
        return None
    if email.lower() != ADMIN_EMAIL:
        return "Bootstrap skipped: ADMIN_EMAIL must match the authorized administrator."
    try:
        if store.find_user(ADMIN_EMAIL):
            return None
        user = store.create_user(
            email, os.getenv("ADMIN_NAME", "Prime IT Club"), password, role="owner"
        )
        return f"Created the first owner account for {user['email']}."
    except Exception:  # weak password, database down, duplicate race
        return "Could not create the bootstrap account; check server configuration."


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


def _request_rate_limit_key(request: Request) -> str:
    """Use the socket peer, not an untrusted forwarded-IP request header."""
    client = request.client
    return client_rate_limit_key(client.host if client else None)


def current_admin(
    request: Request,
    response: Response,
    authorization: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Resolve `Authorization: Bearer <session token>` into the signed-in user."""
    cookie_auth = not authorization
    token = request.cookies.get("prime_admin_session") if cookie_auth else ""
    if authorization:
        if not authorization.lower().startswith("bearer "):
            raise _unauthorised("Sign in to continue.")
        token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise _unauthorised("Sign in to continue.")
    session = store.session_user(token)
    if not session:
        raise _unauthorised("That session has expired. Sign in again.")
    if cookie_auth and request.method not in {"GET", "HEAD", "OPTIONS"}:
        csrf = request.cookies.get("prime_admin_csrf", "")
        supplied = request.headers.get("x-csrf-token", "")
        if (
            not csrf
            or not supplied
            or not session.get("csrf_hash")
            or not hmac.compare_digest(csrf, supplied)
            or not hmac.compare_digest(token_fingerprint(csrf), session["csrf_hash"])
        ):
            raise HTTPException(status_code=403, detail="CSRF validation failed.")
    if cookie_auth:
        _set_admin_cookies(
            response,
            request,
            {
                "token": token,
                "csrf_token": request.cookies.get("prime_admin_csrf", ""),
            },
        )
    return session


def _secure_cookie(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def _set_admin_cookies(
    response: Response, request: Request, session: Dict[str, str]
) -> None:
    response.set_cookie(
        "prime_admin_session",
        session["token"],
        max_age=12 * 60 * 60,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="strict",
        # Also required by the authenticated gate scanner at /api/check-in.
        path="/api",
    )
    response.set_cookie(
        "prime_admin_csrf",
        session["csrf_token"],
        max_age=12 * 60 * 60,
        httponly=False,
        secure=_secure_cookie(request),
        samesite="strict",
        path="/",
    )


def _clear_admin_cookies(response: Response) -> None:
    response.delete_cookie("prime_admin_session", path="/api")
    # Expire the pre-migration path in clients that still hold the old cookie.
    response.delete_cookie("prime_admin_session", path="/api/admin")
    response.delete_cookie("prime_admin_csrf", path="/")
    response.delete_cookie("prime_admin_challenge", path="/api/admin/mfa")


def owner_only(session: Dict[str, Any] = Depends(current_admin)) -> Dict[str, Any]:
    """Event editing and staff management are owner-only."""
    if session["user"]["role"] != "owner":
        raise HTTPException(
            status_code=403, detail="Only an owner can do that."
        )
    return session


def _customer_accounts_enabled() -> bool:
    return os.getenv("CUSTOMER_ACCOUNTS_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def require_customer_feature() -> None:
    if not _customer_accounts_enabled():
        raise HTTPException(status_code=404, detail="Customer accounts are not enabled.")
    if not store.customer_schema_ready():
        raise HTTPException(
            status_code=503,
            detail="Customer account schema has not been explicitly migrated.",
        )


def current_customer(request: Request) -> Dict[str, Any]:
    """Resolve only the dedicated customer cookie; never accept an admin session."""
    require_customer_feature()
    token = request.cookies.get("prime_customer_session", "")
    if not token:
        raise _unauthorised("Sign in to continue.")
    session = store.customer_session_user(token)
    if not session:
        raise _unauthorised("That customer session has expired. Sign in again.")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        csrf = request.cookies.get("prime_customer_csrf", "")
        supplied = request.headers.get("x-customer-csrf-token", "")
        if (
            not csrf
            or not supplied
            or not session.get("csrf_hash")
            or not hmac.compare_digest(csrf, supplied)
            or not hmac.compare_digest(token_fingerprint(csrf), session["csrf_hash"])
        ):
            raise HTTPException(status_code=403, detail="Customer CSRF validation failed.")
    return session


def optional_customer_for_registration(
    request: Request
) -> Optional[Dict[str, Any]]:
    if (
        not _customer_accounts_enabled()
        or not request.cookies.get("prime_customer_session")
    ):
        return None
    return current_customer(request)


def _set_customer_cookies(
    response: Response, request: Request, session: Dict[str, str]
) -> None:
    response.set_cookie(
        "prime_customer_session",
        session["token"],
        max_age=12 * 60 * 60,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="strict",
        path="/api",
    )
    response.set_cookie(
        "prime_customer_csrf",
        session["csrf_token"],
        max_age=12 * 60 * 60,
        httponly=False,
        secure=_secure_cookie(request),
        samesite="strict",
        path="/",
    )


def _clear_customer_cookies(response: Response) -> None:
    response.delete_cookie("prime_customer_session", path="/api")
    response.delete_cookie("prime_customer_csrf", path="/")


def _customer_rate_limit(
    request: Request,
    operation: str,
    subject: str = "",
    maximum: int = MAX_ACTION_ATTEMPTS,
) -> None:
    peer = request.client.host if request.client else "unknown"
    try:
        allowed = store.use_customer_rate_limit(
            f"{operation}:ip:{peer}", operation, maximum
        )
        if subject:
            allowed = (
                store.use_customer_rate_limit(
                    f"{operation}:subject:{subject}", operation, maximum
                )
                and allowed
            )
    except CustomerRateLimitStorageFull as exc:
        raise HTTPException(
            status_code=503,
            detail="Customer security service is temporarily unavailable. Please try again later.",
        ) from exc
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Please wait and try again later.",
        )


def _send_customer_action(
    recipient: str,
    subject: str,
    action: str,
    token: str,
) -> None:
    try:
        send_customer_email(recipient, subject, action, token)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503, detail="Customer email delivery is not configured."
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="Email delivery is temporarily unavailable. Please try again.",
        ) from exc


# --------------------------------------------------------------------------- #
# Customer accounts (separate from administrator authentication)
# --------------------------------------------------------------------------- #
@app.post("/api/customer/register", status_code=202)
def customer_register(
    payload: CustomerRegister,
    request: Request,
    _enabled: None = Depends(require_customer_feature),
):
    _customer_rate_limit(request, "register", payload.email, maximum=5)
    try:
        customer_email_configuration()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503, detail="Customer email delivery is not configured."
        ) from exc
    account, _created = store.register_customer(
        payload.email, payload.display_name
    )
    if account and not account.get("email_verified_at"):
        token = store.create_customer_action(
            int(account["id"]),
            "verify_email",
            timedelta(hours=EMAIL_VERIFY_HOURS),
        )
        _send_customer_action(
            account["email"],
            "Verify your Prime Event Ticket Manager account",
            "verify_email",
            token,
        )
    return {
        "ok": True,
        "message": "If this address can be registered, a verification link will be sent.",
    }


@app.get("/api/customer/availability")
def customer_availability():
    return {
        "enabled": _customer_accounts_enabled() and store.customer_schema_ready()
    }


@app.post("/api/customer/verification/resend", status_code=202)
def customer_resend_verification(
    payload: CustomerEmailRequest,
    request: Request,
    _enabled: None = Depends(require_customer_feature),
):
    _customer_rate_limit(request, "verification", payload.email, maximum=4)
    try:
        customer_email_configuration()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503, detail="Customer email delivery is not configured."
        ) from exc
    account = store.customer_by_email(payload.email)
    if account and not account.get("email_verified_at"):
        token = store.create_customer_action(
            int(account["id"]),
            "verify_email",
            timedelta(hours=EMAIL_VERIFY_HOURS),
        )
        _send_customer_action(
            account["email"],
            "Verify your Prime Event Ticket Manager account",
            "verify_email",
            token,
        )
    return {
        "ok": True,
        "message": "If this address needs verification, a link will be sent.",
    }


@app.post("/api/customer/verify-email")
def customer_verify_email(
    payload: CustomerEmailVerification,
    request: Request,
    _enabled: None = Depends(require_customer_feature),
):
    _customer_rate_limit(request, "verify-email", maximum=10)
    problem = password_problem(payload.new_password)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    if not store.verify_customer_email(payload.token, payload.new_password):
        raise HTTPException(status_code=400, detail="That verification link is invalid or expired.")
    return {"ok": True, "message": "Email verified. You can now sign in."}


@app.post("/api/customer/login")
def customer_login(
    payload: CustomerLogin,
    request: Request,
    response: Response,
    user_agent: Optional[str] = Header(default=None),
    _enabled: None = Depends(require_customer_feature),
):
    _customer_rate_limit(request, "login", payload.email, maximum=MAX_LOGIN_ATTEMPTS)
    result = store.login_customer(payload.email, payload.password, user_agent or "")
    if not result:
        raise _unauthorised("Email/password is invalid or the email is not verified.")
    session, account = result
    _set_customer_cookies(response, request, session)
    return {
        "ok": True,
        "user": store._public_customer(account),
        "expires_at": session["expires_at"],
    }


@app.post("/api/customer/logout")
def customer_logout(
    response: Response,
    session: Dict[str, Any] = Depends(current_customer),
):
    store.end_customer_session(session["token_hash"])
    _clear_customer_cookies(response)
    return {"ok": True}


@app.get("/api/customer/session")
def customer_session(session: Dict[str, Any] = Depends(current_customer)):
    return {
        "ok": True,
        "user": session["user"],
        "expires_at": session["expires_at"],
    }


@app.patch("/api/customer/profile")
def customer_update_profile(
    payload: CustomerProfilePatch,
    session: Dict[str, Any] = Depends(current_customer),
):
    user = store.update_customer_profile(
        int(session["user"]["id"]), payload.display_name
    )
    if not user:
        raise _unauthorised("That customer account is no longer available.")
    return {"ok": True, "user": user}


@app.post("/api/customer/password")
def customer_change_password(
    payload: CustomerPasswordChange,
    request: Request,
    response: Response,
    session: Dict[str, Any] = Depends(current_customer),
):
    customer_id = int(session["user"]["id"])
    _customer_rate_limit(
        request, "password-change", str(customer_id), maximum=MAX_ACTION_ATTEMPTS
    )
    result, rotated = store.change_customer_password(
        customer_id,
        payload.current_password,
        payload.new_password,
    )
    if result == "wrong":
        raise HTTPException(status_code=400, detail="Your current password is not correct.")
    if result == "weak":
        raise HTTPException(status_code=400, detail="That new password is not strong enough.")
    _set_customer_cookies(response, request, rotated)
    return {"ok": True, "message": "Password changed and customer sessions rotated."}


@app.post("/api/customer/password/forgot", status_code=202)
def customer_forgot_password(
    payload: CustomerEmailRequest,
    request: Request,
    _enabled: None = Depends(require_customer_feature),
):
    _customer_rate_limit(request, "password-reset", payload.email, maximum=4)
    try:
        customer_email_configuration()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503, detail="Customer email delivery is not configured."
        ) from exc
    account = store.customer_by_email(payload.email)
    if account and account.get("email_verified_at"):
        token = store.create_customer_action(
            int(account["id"]),
            "password_reset",
            timedelta(minutes=PASSWORD_RESET_MINUTES),
        )
        _send_customer_action(
            account["email"],
            "Reset your Prime Event Ticket Manager password",
            "password_reset",
            token,
        )
    return {
        "ok": True,
        "message": "If a verified account exists for this address, a reset link will be sent.",
    }


@app.post("/api/customer/password/reset")
def customer_reset_password(
    payload: CustomerPasswordReset,
    request: Request,
    _enabled: None = Depends(require_customer_feature),
):
    _customer_rate_limit(request, "password-reset-confirm", maximum=10)
    problem = password_problem(payload.new_password)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    if not store.reset_customer_password(payload.token, payload.new_password):
        raise HTTPException(status_code=400, detail="That reset link is invalid or expired.")
    return {"ok": True, "message": "Password reset. Sign in with your new password."}


@app.get("/api/customer/tickets")
def customer_tickets(session: Dict[str, Any] = Depends(current_customer)):
    items = store.customer_tickets(int(session["user"]["id"]))
    for item in items:
        item["qr_image"] = generate_qr_data_uri(item["token"])
    return {"items": items}


@app.post("/api/customer/tickets/claim", status_code=202)
def customer_request_ticket_claim(
    payload: CustomerTicketClaim,
    request: Request,
    session: Dict[str, Any] = Depends(current_customer),
):
    customer_id = int(session["user"]["id"])
    _customer_rate_limit(
        request, "ticket-claim", str(customer_id), maximum=MAX_ACTION_ATTEMPTS
    )
    ticket = store.customer_ticket_for_claim(payload.ticket_id)
    if (
        ticket
        and not ticket["claimed"]
        and ticket["student_email"].strip().lower()
        == session["user"]["email"].strip().lower()
    ):
        token = store.create_customer_action(
            customer_id,
            "ticket_claim",
            timedelta(minutes=TICKET_CLAIM_MINUTES),
            ticket_token=ticket["token"],
        )
        _send_customer_action(
            ticket["student_email"],
            "Confirm ticket access",
            "ticket_claim",
            token,
        )
    return {
        "ok": True,
        "message": "If this ticket can be claimed, a confirmation link was sent to its registered email.",
    }


@app.post("/api/customer/tickets/claim/confirm")
def customer_confirm_ticket_claim(
    payload: CustomerTokenRequest,
    request: Request,
    _enabled: None = Depends(require_customer_feature),
):
    _customer_rate_limit(request, "ticket-claim-confirm", maximum=10)
    if not store.claim_customer_ticket(payload.token):
        raise HTTPException(status_code=400, detail="That confirmation link is invalid or expired.")
    return {"ok": True, "message": "Ticket added to your account."}


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    """Minimal public liveness response."""
    storage = store.health()
    return JSONResponse(
        {"status": "ok" if storage["ok"] else "unavailable"},
        status_code=200 if storage["ok"] else 503,
    )


@app.get("/api/admin/health")
def admin_health(session: Dict[str, Any] = Depends(owner_only)):
    """Detailed storage diagnostics are restricted to an authenticated owner."""
    return {"storage": store.health()}


@app.get("/api/events")
def list_events(scope: str = "all", category: str = ""):
    events = store.get_events(include_past=(scope != "upcoming"))
    if category and category.lower() != "all":
        events = [e for e in events if e.category.lower() == category.lower()]
    return [e.model_dump() for e in events]


@app.post("/api/register")
def register_student(req: RegistrationRequest, request: Request):
    customer_session = optional_customer_for_registration(request)
    customer_id = (
        int(customer_session["user"]["id"]) if customer_session else None
    )
    ticket, created = store.register_ticket(req, customer_id=customer_id)
    if ticket is not None and not created:
        raise HTTPException(
            status_code=409,
            detail=(
                "Registration could not be confirmed. If you already registered, "
                "use your saved pass or contact event support."
            ),
        )
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
def check_in_ticket(
    token: str = "", session: Dict[str, Any] = Depends(current_admin)
):
    result = store.check_in(token)
    if not result["success"]:
        status = 409 if result.get("already_checked") else 404
        raise HTTPException(status_code=status, detail=result["message"])
    return result


@app.get("/api/stats")
def event_stats(session: Dict[str, Any] = Depends(current_admin)):
    return store.get_stats()


@app.get("/api/export.csv")
def export_csv(event_id: Optional[int] = None, q: str = "", status: str = "all"):
    target = "/api/admin/export.csv"
    params = {}
    if event_id is not None:
        params["event_id"] = event_id
    if q:
        params["q"] = q
    if status != "all":
        params["status"] = status
    if params:
        target += "?" + urlencode(params)
    return RedirectResponse(url=target, status_code=307)


def _ticket_export_response(
    event_id: Optional[int] = None, q: str = "", status: str = "all"
):
    tickets = store.all_tickets(event_id, query=q, status=status)
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
def admin_login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    user_agent: Optional[str] = Header(default=None),
):
    """Validate the password factor and issue a short-lived MFA challenge."""
    if not store.find_user(ADMIN_EMAIL):
        # A cold deploy may have started before the database was reachable.
        bootstrap_admin()
    if not store.find_user(ADMIN_EMAIL):
        raise HTTPException(
            status_code=503,
            detail=(
                "The authorized admin account is not configured. Set ADMIN_EMAIL to the "
                "allowlisted email and configure ADMIN_PASSWORD on the API host."
            ),
        )

    rate_limit_key = _request_rate_limit_key(request)
    result = store.login(
        payload.email,
        payload.password,
        user_agent=user_agent or "",
        rate_limit_key=rate_limit_key,
    )
    status = result["status"]
    if status == "mfa_required":
        response.set_cookie(
            "prime_admin_challenge",
            result["challenge"],
            max_age=5 * 60,
            httponly=True,
            secure=_secure_cookie(request),
            samesite="strict",
            path="/api/admin/mfa",
        )
        return {
            "ok": True,
            "mfa_required": True,
            "enrollment_required": result["enrollment_required"],
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


def _challenge_or_unauthorized(request: Request) -> Dict[str, Any]:
    challenge = request.cookies.get("prime_admin_challenge", "")
    row = store.mfa_challenge(challenge)
    if not row:
        raise _unauthorised("That sign-in challenge has expired. Sign in again.")
    rate_limit_key = _request_rate_limit_key(request)
    wait = max(
        throttle_remaining(rate_limit_key),
        store.auth_throttle_remaining(rate_limit_key),
        store.mfa_throttle_remaining(int(row["id"])),
    )
    if wait or int(row["attempts"]) >= MAX_MFA_FAILURES:
        wait = wait or MFA_CHALLENGE_SECONDS
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {wait} seconds.",
            headers={"Retry-After": str(wait)},
        )
    return row


@app.post("/api/admin/mfa/enroll/start")
def admin_mfa_enroll_start(request: Request):
    _challenge_or_unauthorized(request)
    challenge = request.cookies.get("prime_admin_challenge", "")
    try:
        enrollment = store.begin_totp_enrollment(challenge)
    except MFAConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise _unauthorised(str(exc)) from exc
    return {
        **enrollment,
        "qr_code": generate_qr_data_uri(enrollment["provisioning_uri"]),
    }


@app.post("/api/admin/mfa/enroll/activate")
def admin_mfa_enroll_activate(
    payload: TotpCode,
    request: Request,
    response: Response,
    user_agent: Optional[str] = Header(default=None),
):
    _challenge_or_unauthorized(request)
    try:
        result = store.activate_totp(
            request.cookies.get("prime_admin_challenge", ""),
            payload.code,
            user_agent or "",
            rate_limit_key=_request_rate_limit_key(request),
        )
    except MFAConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if result["status"] == "throttled":
        wait = int(result["retry_after"])
        raise HTTPException(
            status_code=429,
            detail=f"Too many MFA attempts. Try again in {wait} seconds.",
            headers={"Retry-After": str(wait)},
        )
    if result["status"] != "ok":
        rate_limit_key = _request_rate_limit_key(request)
        note_failure(rate_limit_key)
        store.note_auth_failure(rate_limit_key)
        raise _unauthorised("That authenticator code is invalid. Try the current code.")
    _set_admin_cookies(response, request, result["session"])
    response.delete_cookie("prime_admin_challenge", path="/api/admin/mfa")
    return {
        "ok": True,
        "user": result["user"],
        "expires_at": result["session"]["expires_at"],
        "recovery_codes": result["recovery_codes"],
    }


@app.post("/api/admin/mfa/verify")
def admin_mfa_verify(
    payload: MFAVerify,
    request: Request,
    response: Response,
    user_agent: Optional[str] = Header(default=None),
):
    _challenge_or_unauthorized(request)
    use_recovery = not (payload.code.isdigit() and len(payload.code) == 6)
    try:
        result = store.complete_mfa(
            request.cookies.get("prime_admin_challenge", ""),
            payload.code,
            user_agent or "",
            recovery=use_recovery,
            rate_limit_key=_request_rate_limit_key(request),
        )
    except MFAConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if result["status"] == "throttled":
        wait = int(result["retry_after"])
        raise HTTPException(
            status_code=429,
            detail=f"Too many MFA attempts. Try again in {wait} seconds.",
            headers={"Retry-After": str(wait)},
        )
    if result["status"] != "ok":
        rate_limit_key = _request_rate_limit_key(request)
        note_failure(rate_limit_key)
        store.note_auth_failure(rate_limit_key)
        raise _unauthorised(
            "That recovery code is invalid or already used."
            if use_recovery
            else "That authenticator code is invalid or already used."
        )
    _set_admin_cookies(response, request, result["session"])
    response.delete_cookie("prime_admin_challenge", path="/api/admin/mfa")
    return {
        "ok": True,
        "user": result["user"],
        "expires_at": result["session"]["expires_at"],
    }


@app.post("/api/admin/logout")
def admin_logout(
    response: Response, session: Dict[str, Any] = Depends(current_admin)
):
    store.end_session(session["token_hash"])
    _clear_admin_cookies(response)
    return {"ok": True}


@app.get("/api/admin/session")
def admin_session(session: Dict[str, Any] = Depends(current_admin)):
    return {"ok": True, "user": session["user"], "expires_at": session["expires_at"]}


@app.patch("/api/admin/profile")
def admin_update_profile(
    payload: ProfileUpdate, session: Dict[str, Any] = Depends(current_admin)
):
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    if payload.preferences is not None:
        changes["preferences"] = payload.preferences.model_dump()
    user = store.update_user(session["user"]["id"], changes)
    return {"ok": True, "user": user}


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
    person = store.find_user(session["user"]["email"])
    return {
        "items": [store._public_user(person)] if person else [],
        "roles": ["owner"],
    }


@app.post("/api/admin/users", status_code=201)
def admin_create_user(payload: UserCreate, session: Dict[str, Any] = Depends(owner_only)):
    raise HTTPException(
        status_code=403, detail="Creating or inviting additional administrators is disabled."
    )


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(
    user_id: int, patch: UserPatch, session: Dict[str, Any] = Depends(owner_only)
):
    if user_id != session["user"]["id"]:
        raise HTTPException(
            status_code=403, detail="Managing additional administrator accounts is disabled."
        )
    changes = patch.model_dump(exclude_unset=True, exclude_none=True)
    if "role" in changes or "active" in changes:
        raise HTTPException(
            status_code=403, detail="Changing administrator access is disabled."
        )
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
    try:
        updated = store.update_event(event_id, patch)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Dict[str, Any] = Depends(current_admin),
):
    return store.attendees(event_id=event_id, query=q, status=status, limit=limit, offset=offset)


@app.get("/api/admin/export.csv")
def admin_export_csv(
    event_id: Optional[int] = None,
    q: str = "",
    status: str = "all",
    session: Dict[str, Any] = Depends(current_admin),
):
    return _ticket_export_response(event_id, q, status)


@app.post("/api/admin/undo-check-in")
def admin_undo_check_in(
    token: str = "",
    session: Dict[str, Any] = Depends(current_admin),
):
    """Reverse a check-in the door got wrong. Any signed-in coordinator may do this."""
    result = store.undo_check_in(token)
    if not result["success"]:
        status = 409 if result.get("not_checked_in") else 404
        raise HTTPException(status_code=status, detail=result["message"])
    return result


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
