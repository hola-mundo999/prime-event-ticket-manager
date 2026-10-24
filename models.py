"""Data models and the storage layer for the Prime IT Club ticket system.

The store runs on SQLite for local work and on Postgres (Supabase) in
production, so registrations survive redeploys.

The event catalog follows what Prime IT Club actually runs: the ICT Meetup
series, Hackaverse, the Prime IT Job Fair, the Shrijay Memorial ICT Cup and
their workshop and panel programmes. Dates for past editions are the real
ones; the upcoming editions carry the next date of each recurring programme
so the ticket flow can be demonstrated end to end.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

import db as db_adapter

DB_PATH = os.getenv("TICKET_DB_PATH", "event_tickets.db")

# Nepal is UTC+05:45 and never observes daylight saving.
NEPAL_TZ = timedelta(hours=5, minutes=45)


def _nepal_now() -> datetime:
    """Aware 'now' pinned to Nepal's offset, whatever the host clock is set to."""
    return datetime.now(timezone(NEPAL_TZ))


DEFAULT_CAPACITY = 100


# --------------------------------------------------------------------------- #
# Event catalog (real Prime IT Club programmes)
# --------------------------------------------------------------------------- #
# Each entry is the club's own programme, not a generic sample. Past rows use
# the dates the events actually ran; the final block is the next edition of
# each recurring programme, so registration and check-in can be exercised.
CATALOG: List[Dict[str, Any]] = [
    {
        "slug": "ict-meetup-v6-0",
        "title": "ICT Meetup V6.0",
        "category": "Tech Fest",
        "starts_at": "2019-12-12T09:00:00+05:45",
        "ends_at": "2019-12-14T18:00:00+05:45",
        "venue": "Khusibun, Kathmandu",
        "capacity": 400,
        "speaker": "Prime IT Club & industry speakers",
        "description": "The sixth ICT Meetup ran on the theme Advancing Towards Smart City for "
        "Sustainable Development, with project demos, panel discussions, parallel "
        "workshops on AI, IoT and Spark AR, and a photography competition.",
    },
    {
        "slug": "ict-meetup-v7-0",
        "title": "ICT Meetup V7.0",
        "category": "Tech Fest",
        "starts_at": "2024-01-08T09:00:00+05:45",
        "ends_at": "2024-01-09T20:00:00+05:45",
        "venue": "Prime College, Khusibu, Nayabazar",
        "capacity": 500,
        "speaker": "Prime IT Club, mentors and judges panel",
        "description": "Two days on the theme AI Innovation: Sculpting a Smarter Tomorrow with "
        "an ideathon, AI sessions and workshops, project demos, graphic design and "
        "esports competitions, a panel discussion and a closing musical night.",
    },
    {
        "slug": "ict-meetup-v8-0",
        "title": "ICT Meetup V8.0",
        "category": "Tech Fest",
        "starts_at": "2026-09-05T09:00:00+05:45",
        "ends_at": "2026-09-07T18:00:00+05:45",
        "venue": "Prime College, Khusibun",
        "capacity": 500,
        "speaker": "Prime IT Club core team",
        "description": "Three days of the club's flagship meetup: technical sessions, hands-on "
        "workshops, competitions, project showcases and networking across the IT "
        "community in Kathmandu.",
    },
    {
        "slug": "hackaverse-genesis",
        "title": "Hackaverse: Genesis",
        "category": "Hackathon",
        "starts_at": "2024-07-20T08:00:00+05:45",
        "ends_at": "2024-07-21T20:00:00+05:45",
        "venue": "Prime College, Nayabazar",
        "capacity": 120,
        "speaker": "Mentors & judges panel",
        "description": "The first Hackaverse: student teams spent a weekend turning ideas into "
        "working prototypes for campus life, education and community problems.",
    },
    {
        "slug": "hackaverse-hack-the-hassle",
        "title": "Hackaverse: Hack the Hassle",
        "category": "Hackathon",
        "starts_at": "2025-06-14T08:00:00+05:45",
        "ends_at": "2025-06-15T14:00:00+05:45",
        "venue": "Prime College, Nayabazar",
        "capacity": 150,
        "speaker": "Industry mentors & judging panel",
        "description": "A 30 hour hackathon on the theme Micro Problem, Macro Impact, where teams "
        "built small, usable fixes for everyday hassles and pitched them to a panel.",
    },
    {
        "slug": "prime-it-job-fair-2025",
        "title": "Prime IT Job Fair 2025",
        "category": "Career",
        "starts_at": "2025-06-14T10:00:00+05:45",
        "ends_at": "2025-06-14T17:00:00+05:45",
        "venue": "Prime College, Nayabazar",
        "capacity": 300,
        "speaker": "Partner companies & HR teams",
        "description": "Company booths, on the spot interviews, CV clinics and networking that "
        "connected students and job seekers with hiring teams from across Kathmandu.",
    },
    {
        "slug": "shrijay-memorial-ict-cup-2025",
        "title": "Shrijay Memorial ICT Cup 2025",
        "category": "Sports",
        "starts_at": "2025-01-12T09:00:00+05:45",
        "ends_at": "2025-01-19T17:00:00+05:45",
        "venue": "Prime College futsal ground",
        "capacity": 160,
        "speaker": "Prime IT Club sports desk",
        "description": "The second edition of the futsal tournament that brings IT companies, "
        "students and alumni onto the same pitch for a day of community and teamwork.",
    },
    {
        "slug": "git-github-workshop-2025",
        "title": "Git & GitHub Workshop",
        "category": "Workshop",
        "starts_at": "2025-02-02T11:00:00+05:45",
        "ends_at": "2025-02-02T14:00:00+05:45",
        "venue": "Seminar Hall, Prime College",
        "capacity": 80,
        "speaker": "Prime IT Club tech team",
        "description": "A hands-on introduction to version control: branching, pull requests and "
        "the collaborative workflow every hackathon team needs on day one.",
    },
    {
        "slug": "pitch-deck-workshop-2024",
        "title": "How To Prepare A Pitch Deck?",
        "category": "Workshop",
        "starts_at": "2024-02-11T11:00:00+05:45",
        "ends_at": "2024-02-11T13:00:00+05:45",
        "venue": "Prime College, Khusibu",
        "capacity": 60,
        "speaker": "Startup mentors",
        "description": "A practical session on business planning and on building and presenting a "
        "pitch deck that survives first contact with a judging panel.",
    },
    {
        "slug": "iwd-conclave-invest-in-her",
        "title": "IWD Conclave: Invest in Her",
        "category": "Panel",
        "starts_at": "2024-03-02T10:00:00+05:45",
        "ends_at": "2024-03-02T13:00:00+05:45",
        "venue": "Prime College, Khusibu",
        "capacity": 120,
        "speaker": "Prime IT Club & WIIT",
        "description": "An International Women's Day discussion on Invest in Her: Men as "
        "Co-partners to Empower Women in ICT, run together with Women in IT.",
    },
    {
        "slug": "yathartha-her-voice-in-stem",
        "title": "Yathartha: Her Voice in STEM",
        "category": "Panel",
        "starts_at": "2025-03-08T11:00:00+05:45",
        "ends_at": "2025-03-08T14:00:00+05:45",
        "venue": "Prime College, Nayabazar",
        "capacity": 100,
        "speaker": "Prime IT Club & Women Leaders in Technology",
        "description": "A women's day conversation with women leaders in technology about their "
        "routes into STEM, the barriers they met and the advice they give.",
    },
    {
        "slug": "career-mentorship-session-2024",
        "title": "Career Mentorship Session",
        "category": "Session",
        "starts_at": "2024-08-24T10:00:00+05:45",
        "ends_at": "2024-08-24T13:00:00+05:45",
        "venue": "Prime College, Khusibu",
        "capacity": 60,
        "speaker": "Professionals in data science, AI/ML, design, security and marketing",
        "description": "Round table mentoring with working professionals across data science, "
        "AI/ML, design, cybersecurity, blockchain and digital marketing.",
    },
    {
        "slug": "empowering-girls-ms-office-2024",
        "title": "Empowering Girls through Tech: MS Office",
        "category": "Workshop",
        "starts_at": "2024-07-15T10:00:00+05:45",
        "ends_at": "2024-07-20T13:00:00+05:45",
        "venue": "Kanya Mandir School, Kathmandu",
        "capacity": 40,
        "speaker": "Prime IT Club volunteers & Voices of Women Media",
        "description": "A six day workshop run with Voices of Women Media that taught MS Office "
        "skills to Grade 11 students at Kanya Mandir School.",
    },
    # ---- next editions of the recurring programmes (open for registration) --
    {
        "slug": "hackaverse-2026",
        "title": "Hackaverse 2026: Build for Campus",
        "category": "Hackathon",
        "starts_at": "2026-11-21T08:00:00+05:45",
        "ends_at": "2026-11-22T20:00:00+05:45",
        "venue": "Prime College, Nayabazar",
        "capacity": 150,
        "speaker": "Industry mentors & judging panel",
        "description": "The next Hackaverse: a 36 hour build sprint for tools that make campus life "
        "easier, with mentor checkpoints, an open demo floor and a judged final pitch.",
    },
    {
        "slug": "ai-llm-workshop-2026",
        "title": "AI & LLM Hands-on Workshop",
        "category": "Workshop",
        "starts_at": "2026-11-15T10:00:00+05:45",
        "ends_at": "2026-11-15T13:00:00+05:45",
        "venue": "Computer Lab 3, Prime College",
        "capacity": 60,
        "speaker": "Industry AI engineers",
        "description": "Practical session on prompt engineering, embeddings and vector search, "
        "finishing with a working AI assistant built in Python.",
    },
    {
        "slug": "git-github-workshop-2026",
        "title": "Git, GitHub & Open Source Bootcamp",
        "category": "Bootcamp",
        "starts_at": "2026-12-05T11:00:00+05:45",
        "ends_at": "2026-12-05T14:30:00+05:45",
        "venue": "Seminar Hall, Prime College",
        "capacity": 90,
        "speaker": "Prime IT Club tech team",
        "description": "From first commit to your first merged pull request: branching strategies, "
        "review etiquette and how to find an open source issue worth fixing.",
    },
    {
        "slug": "prime-it-job-fair-2026",
        "title": "Prime IT Job Fair 2026",
        "category": "Career",
        "starts_at": "2026-12-12T10:00:00+05:45",
        "ends_at": "2026-12-12T16:00:00+05:45",
        "venue": "Prime College, Nayabazar",
        "capacity": 300,
        "speaker": "Partner companies & HR teams",
        "description": "Recruiters from Kathmandu's IT companies meet students for booth chats, "
        "portfolio reviews and on the spot interviews across one afternoon.",
    },
]

# Rows from the original demo seed. They are mapped onto the catalog entry with
# the same intent so an existing database keeps its tickets and gains the real
# event text instead of showing a duplicate.
LEGACY_TITLES = {
    "AI & LLM Hands-on Workshop": "ai-llm-workshop-2026",
    "Git, GitHub & Open Source Bootcamp": "git-github-workshop-2026",
}
LEGACY_UNMAPPED = ("Prime CSIT Intra-College Hackathon",)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class Event(BaseModel):
    id: int
    title: str
    slug: str = ""
    category: str
    date: str
    time: str
    venue: str
    capacity: int
    registered_count: int = 0
    seats_left: int = 0
    status: str = "upcoming"
    speaker: str
    description: str
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None


class RegistrationRequest(BaseModel):
    event_id: int
    student_name: str = Field(min_length=1, max_length=120)
    student_email: EmailStr
    college_id: str = Field(min_length=1, max_length=40)

    @field_validator("student_name", "college_id")
    @classmethod
    def strip_values(cls, v: str) -> str:
        return v.strip()

    @field_validator("student_email")
    @classmethod
    def lower_email(cls, v: str) -> str:
        return str(v).strip().lower()


class Ticket(BaseModel):
    ticket_id: str
    token: str
    event_id: int
    event_title: str
    student_name: str
    student_email: str
    college_id: str
    is_checked_in: bool = False
    check_in_time: Optional[str] = None
    created_at: str


class EventInput(BaseModel):
    """Payload for creating or editing an event from the admin panel."""

    title: str = Field(min_length=3, max_length=120)
    category: str = Field(min_length=2, max_length=40)
    venue: str = Field(min_length=2, max_length=120)
    capacity: int = Field(ge=1, le=10000)
    speaker: str = Field(default="Prime IT Club", max_length=160)
    description: str = Field(default="", max_length=2000)
    starts_at: str = Field(description="ISO 8601 start, e.g. 2026-11-21T08:00:00+05:45")
    ends_at: Optional[str] = None

    @field_validator("title", "category", "venue", "speaker", "description")
    @classmethod
    def strip_text(cls, v: str) -> str:
        return (v or "").strip()

    @field_validator("starts_at", "ends_at")
    @classmethod
    def valid_iso(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("must be an ISO 8601 date such as 2026-11-21T08:00") from exc
        return v


class EventPatch(BaseModel):
    title: Optional[str] = Field(default=None, min_length=3, max_length=120)
    category: Optional[str] = Field(default=None, min_length=2, max_length=40)
    venue: Optional[str] = Field(default=None, min_length=2, max_length=120)
    capacity: Optional[int] = Field(default=None, ge=1, le=10000)
    speaker: Optional[str] = Field(default=None, max_length=160)
    description: Optional[str] = Field(default=None, max_length=2000)
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_nepal_now().tzinfo)
    return parsed


def pretty_date(start: Optional[datetime], end: Optional[datetime]) -> str:
    if not start:
        return ""
    if end and end < start:
        end = start
    if end and (end.date() != start.date()):
        if (end.year, end.month) == (start.year, start.month):
            return f"{start.strftime('%b %d')} to {end.strftime('%d, %Y')}"
        return f"{start.strftime('%b %d, %Y')} to {end.strftime('%b %d, %Y')}"
    return start.strftime("%b %d, %Y")


def pretty_time(start: Optional[datetime], end: Optional[datetime]) -> str:
    if not start:
        return ""
    label = start.strftime("%I:%M %p").lstrip("0")
    if not end:
        return label
    return f"{label} to {end.strftime('%I:%M %p').lstrip('0')}"


def event_status(starts_at: Optional[str], ends_at: Optional[str]) -> str:
    start = parse_iso(starts_at)
    if not start:
        return "upcoming"
    now = _nepal_now()
    end = parse_iso(ends_at) or start + timedelta(hours=4)
    if now < start:
        return "upcoming"
    if now <= end:
        return "live"
    return "past"


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
class DataStore:
    """Event and ticket store with an admin-friendly query set.

    SQLite by default (``TICKET_DB_PATH``); Postgres when ``DATABASE_URL`` or
    one of Supabase's POSTGRES_* variables is set.
    """

    def __init__(self, db_path: Optional[str] = None, url: str = ""):
        self.db = db_adapter.Database(
            url=url or db_adapter.database_url(),
            sqlite_path=db_path or DB_PATH,
        )
        self.db_path = self.db.sqlite_path
        self._bootstrap()

    @property
    def backend(self) -> str:
        return self.db.dialect

    def _bootstrap(self) -> None:
        """Create the schema and seed the catalog, retrying a cold database."""
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                self._init_db()
                self._migrate()
                self._sync_catalog()
                return
            except Exception as exc:  # network hiccup, pooler waking up, ...
                last_error = exc
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(
            f"Could not prepare the {self.db.dialect} database at {self.db.target}: {last_error}"
        )

    # -- connection -------------------------------------------------------- #
    def _connect(self) -> Any:
        return self.db.connect()

    def _begin_write(self, conn: Any, key: str) -> None:
        """Serialise concurrent writes for one event (or one ticket)."""
        if self.db.dialect == "postgres":
            # Transaction-scoped advisory lock: released on commit/rollback.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (key,))
        else:
            conn.execute("BEGIN IMMEDIATE")

    # -- schema ------------------------------------------------------------ #
    def _init_db(self) -> None:
        self.db.create_schema()

    def _migrate(self) -> None:
        """Add columns that older databases do not have yet."""
        self.db.add_missing_columns()
        conn = self._connect()
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_slug ON events(slug) "
                "WHERE slug IS NOT NULL AND slug <> ''"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tickets_email ON tickets(student_email)"
            )
            conn.execute(
                "UPDATE tickets SET created_ms = created_at WHERE created_ms IS NULL"
            )
            conn.commit()
        finally:
            conn.close()

    def _sync_catalog(self) -> None:
        """Make sure every Prime IT Club programme exists, without touching edits."""
        conn = self._connect()
        try:
            # Carry over the two demo rows that became real catalog entries.
            for legacy_title, slug in LEGACY_TITLES.items():
                row = conn.execute(
                    "SELECT id FROM events WHERE title = ? AND (slug IS NULL OR slug = '')",
                    (legacy_title,),
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE events SET slug = ? WHERE id = ?", (slug, row["id"])
                    )
            # Drop the leftover demo row only when nobody registered for it.
            for title in LEGACY_UNMAPPED:
                row = conn.execute(
                    "SELECT e.id FROM events e WHERE e.title = ? "
                    "AND (SELECT COUNT(*) FROM tickets t WHERE t.event_id = e.id) = 0",
                    (title,),
                ).fetchone()
                if row:
                    conn.execute("DELETE FROM events WHERE id = ?", (row["id"],))

            for item in CATALOG:
                start = parse_iso(item["starts_at"])
                end = parse_iso(item["ends_at"])
                date_label = pretty_date(start, end)
                time_label = pretty_time(start, end)
                exists = conn.execute(
                    "SELECT id, starts_at FROM events WHERE slug = ?", (item["slug"],)
                ).fetchone()
                if exists:
                    if not exists["starts_at"]:
                        # A row carried over from the demo seed: adopt the real
                        # programme text once, keeping its tickets.
                        conn.execute(
                            "UPDATE events SET title = ?, category = ?, venue = ?, "
                            "speaker = ?, description = ?, capacity = ?, starts_at = ?, "
                            "ends_at = ?, date = ?, time = ? WHERE id = ?",
                            (
                                item["title"],
                                item["category"],
                                item["venue"],
                                item["speaker"],
                                item["description"],
                                item["capacity"],
                                item["starts_at"],
                                item["ends_at"],
                                date_label,
                                time_label,
                                exists["id"],
                            ),
                        )
                    continue
                same_title = conn.execute(
                    "SELECT id FROM events WHERE title = ?", (item["title"],)
                ).fetchone()
                if same_title:
                    conn.execute(
                        "UPDATE events SET slug = ?, starts_at = ?, ends_at = ?, date = ?, "
                        "time = ?, category = ?, venue = ?, speaker = ?, description = ? "
                        "WHERE id = ?",
                        (
                            item["slug"],
                            item["starts_at"],
                            item["ends_at"],
                            date_label,
                            time_label,
                            item["category"],
                            item["venue"],
                            item["speaker"],
                            item["description"],
                            same_title["id"],
                        ),
                    )
                    continue
                conn.execute(
                    """INSERT INTO events (title, slug, category, date, time, venue,
                                           capacity, registered_count, speaker,
                                           description, starts_at, ends_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
                    (
                        item["title"],
                        item["slug"],
                        item["category"],
                        date_label,
                        time_label,
                        item["venue"],
                        item["capacity"],
                        item["speaker"],
                        item["description"],
                        item["starts_at"],
                        item["ends_at"],
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    # -- row mapping ------------------------------------------------------- #
    def _registered_count(self, conn: Any, event_id: int) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM tickets WHERE event_id = ?", (event_id,)
        ).fetchone()
        return int(row["c"])

    def _row_to_event(self, row: Dict[str, Any], registered: Optional[int] = None) -> Event:
        registered = row["registered_count"] if registered is None else registered
        capacity = int(row["capacity"])
        starts_at = row["starts_at"] if "starts_at" in row.keys() else None
        ends_at = row["ends_at"] if "ends_at" in row.keys() else None
        return Event(
            id=row["id"],
            title=row["title"],
            slug=row["slug"] or "",
            category=row["category"],
            date=row["date"],
            time=row["time"],
            venue=row["venue"],
            capacity=capacity,
            registered_count=registered,
            seats_left=max(0, capacity - registered),
            status=event_status(starts_at, ends_at),
            speaker=row["speaker"],
            description=row["description"],
            starts_at=starts_at,
            ends_at=ends_at,
        )

    def _row_to_ticket(self, row: Dict[str, Any]) -> Ticket:
        return Ticket(
            ticket_id=row["ticket_id"],
            token=row["token"],
            event_id=row["event_id"],
            event_title=row["event_title"],
            student_name=row["student_name"],
            student_email=row["student_email"],
            college_id=row["college_id"],
            is_checked_in=bool(row["is_checked_in"]),
            check_in_time=row["check_in_time"],
            created_at=row["created_at"],
        )

    # -- reads ------------------------------------------------------------- #
    def get_events(self, include_past: bool = True) -> List[Event]:
        conn = self._connect()
        try:
            # One query with the ticket count inline: on a hosted Postgres a
            # query per event would mean one network round trip per event.
            rows = conn.execute(
                "SELECT e.*, "
                "(SELECT COUNT(*) FROM tickets t WHERE t.event_id = e.id) AS registered "
                "FROM events e"
            ).fetchall()
            events = [
                self._row_to_event(row, int(row["registered"])) for row in rows
            ]
        finally:
            conn.close()

        def sort_key(event: Event):
            start = parse_iso(event.starts_at)
            return (start.timestamp() if start else 0, event.id)

        if include_past:
            return sorted(events, key=sort_key)
        return sorted([e for e in events if e.status != "past"], key=sort_key)

    def get_event(self, event_id: int) -> Optional[Event]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT e.*, "
                "(SELECT COUNT(*) FROM tickets t WHERE t.event_id = e.id) AS registered "
                "FROM events e WHERE e.id = ?",
                (event_id,),
            ).fetchone()
            if not row:
                return None
            return self._row_to_event(row, int(row["registered"]))
        finally:
            conn.close()

    def _event_has_capacity(self, conn: Any, event_id: int) -> bool:
        row = conn.execute(
            "SELECT capacity FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if not row:
            return False
        return self._registered_count(conn, event_id) < int(row["capacity"])

    # -- public registration ------------------------------------------------ #
    def register_ticket(
        self, req: RegistrationRequest
    ) -> tuple[Optional[Ticket], bool]:
        """Register a student.

        Returns (ticket, created). A repeat sign-up returns the existing ticket
        with created=False so the page can say the pass was already issued.
        """
        conn = self._connect()
        try:
            self._begin_write(conn, f"event-{req.event_id}")
            event = conn.execute(
                "SELECT * FROM events WHERE id = ?", (req.event_id,)
            ).fetchone()
            if not event:
                conn.rollback()
                return None, False

            existing = conn.execute(
                """SELECT * FROM tickets
                   WHERE event_id = ? AND lower(student_email) = ? AND college_id = ?""",
                (req.event_id, str(req.student_email).lower(), req.college_id),
            ).fetchone()
            if existing:
                conn.rollback()
                return self._row_to_ticket(existing), False

            if not self._event_has_capacity(conn, event["id"]):
                conn.rollback()
                return None, False

            ticket_id = f"PRIME-{uuid.uuid4().hex[:6].upper()}"
            token = uuid.uuid4().hex[:10].upper()
            now = _nepal_now()
            created_at = now.strftime("%Y-%m-%d %H:%M:%S")
            created_ms = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

            conn.execute(
                """INSERT INTO tickets (token, ticket_id, event_id, event_title,
                                        student_name, student_email, college_id,
                                        is_checked_in, check_in_time, created_at,
                                        created_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, FALSE, NULL, ?, ?)""",
                (
                    token,
                    ticket_id,
                    event["id"],
                    event["title"],
                    req.student_name,
                    str(req.student_email).lower(),
                    req.college_id,
                    created_at,
                    created_ms,
                ),
            )
            conn.commit()
            return Ticket(
                ticket_id=ticket_id,
                token=token,
                event_id=event["id"],
                event_title=event["title"],
                student_name=req.student_name,
                student_email=str(req.student_email).lower(),
                college_id=req.college_id,
                is_checked_in=False,
                check_in_time=None,
                created_at=created_at,
            ), True
        except Exception as exc:
            conn.rollback()
            if self._is_integrity_error(exc):
                return None, False
            raise
        finally:
            conn.close()

    @staticmethod
    def _is_integrity_error(exc: Exception) -> bool:
        """Unique-key clash on either engine."""
        if isinstance(exc, sqlite3.IntegrityError):
            return True
        return exc.__class__.__name__ in {
            "UniqueViolation",
            "IntegrityError",
            "ForeignKeyViolation",
        }

    def check_in(self, token: str) -> Dict[str, Any]:
        clean_token = (token or "").strip().upper()
        conn = self._connect()
        try:
            self._begin_write(conn, f"token-{clean_token}")
            sql = "SELECT * FROM tickets WHERE token = ?"
            if self.db.dialect == "postgres":
                # Row lock so two scanners cannot both accept the same pass.
                sql += " FOR UPDATE"
            row = conn.execute(sql, (clean_token,)).fetchone()
            if not row:
                conn.rollback()
                return {"success": False, "message": "Invalid ticket token. No record found."}

            if bool(row["is_checked_in"]):
                conn.rollback()
                return {
                    "success": False,
                    "already_checked": True,
                    "message": f"Ticket was already checked in at {row['check_in_time']}.",
                    "ticket": self._row_to_ticket(row),
                }

            check_in_time = _nepal_now().strftime("%H:%M:%S")
            conn.execute(
                "UPDATE tickets SET is_checked_in = TRUE, check_in_time = ? WHERE token = ?",
                (check_in_time, clean_token),
            )
            conn.commit()
            ticket = self._row_to_ticket(row)
            ticket.is_checked_in = True
            ticket.check_in_time = check_in_time
            return {
                "success": True,
                "message": f"Verified. Welcome, {ticket.student_name}.",
                "ticket": ticket,
            }
        finally:
            conn.close()

    # -- analytics --------------------------------------------------------- #
    def get_stats(self) -> Dict[str, Any]:
        conn = self._connect()
        try:
            total_registered = conn.execute("SELECT COUNT(*) AS c FROM tickets").fetchone()["c"]
            total_checked_in = conn.execute(
                "SELECT COUNT(*) AS c FROM tickets WHERE is_checked_in = TRUE"
            ).fetchone()["c"]
            rows = conn.execute(
                "SELECT * FROM tickets "
                "ORDER BY COALESCE(created_ms, created_at) DESC, token DESC LIMIT 10"
            ).fetchall()
            recent = [self._row_to_ticket(r) for r in rows]
        finally:
            conn.close()
        events = self.get_events()
        return {
            "total_registered": total_registered,
            "total_checked_in": total_checked_in,
            "total_events": len(events),
            "upcoming_events": len([e for e in events if e.status == "upcoming"]),
            "attendance_rate": round(
                (total_checked_in / total_registered * 100) if total_registered else 0, 1
            ),
            "recent_tickets": recent,
        }

    def analytics(self) -> Dict[str, Any]:
        """Per-event numbers for the admin dashboard."""
        events = self.get_events()
        checked = self._checked_in_map()
        rows = []
        for event in events:
            rows.append(
                {
                    "id": event.id,
                    "title": event.title,
                    "category": event.category,
                    "date": event.date,
                    "status": event.status,
                    "capacity": event.capacity,
                    "registered": event.registered_count,
                    "seats_left": event.seats_left,
                    "checked_in": checked.get(event.id, 0),
                    "fill_rate": round(
                        (event.registered_count / event.capacity * 100)
                        if event.capacity
                        else 0,
                        1,
                    ),
                }
            )
        total_registered = sum(r["registered"] for r in rows)
        total_checked_in = sum(r["checked_in"] for r in rows)
        return {
            "events": rows,
            "totals": {
                "events": len(rows),
                "registered": total_registered,
                "checked_in": total_checked_in,
                "seats": sum(r["capacity"] for r in rows),
                "attendance_rate": round(
                    (total_checked_in / total_registered * 100)
                    if total_registered
                    else 0,
                    1,
                ),
            },
        }

    def _checked_in_map(self) -> Dict[int, int]:
        """checked-in count per event, in one round trip."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT event_id, COUNT(*) AS c FROM tickets "
                "WHERE is_checked_in = TRUE GROUP BY event_id"
            ).fetchall()
            return {int(row["event_id"]): int(row["c"]) for row in rows}
        finally:
            conn.close()

    def _checked_in_for(self, event_id: int) -> int:
        conn = self._connect()
        try:
            return int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM tickets WHERE event_id = ? "
                    "AND is_checked_in = TRUE",
                    (event_id,),
                ).fetchone()["c"]
            )
        finally:
            conn.close()

    # -- admin: attendees -------------------------------------------------- #
    def attendees(
        self,
        event_id: Optional[int] = None,
        query: str = "",
        status: str = "all",
        limit: int = 200,
        offset: int = 0,
    ) -> Dict[str, Any]:
        clauses = []
        params: List[Any] = []
        if event_id:
            clauses.append("event_id = ?")
            params.append(event_id)
        if query:
            like = f"%{query.strip().lower()}%"
            clauses.append(
                "(lower(student_name) LIKE ? OR lower(student_email) LIKE ? "
                "OR lower(college_id) LIKE ? OR upper(token) LIKE ?)"
            )
            params.extend([like, like, like, f"%{query.strip().upper()}%"])
        if status == "checked":
            clauses.append("is_checked_in = TRUE")
        elif status == "pending":
            clauses.append("is_checked_in = FALSE")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        conn = self._connect()
        try:
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM tickets {where}", params
            ).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM tickets {where} "
                f"ORDER BY COALESCE(created_ms, created_at) DESC, token DESC "
                f"LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            return {
                "total": total,
                "items": [self._row_to_ticket(r).model_dump() for r in rows],
            }
        finally:
            conn.close()

    # -- admin: event CRUD -------------------------------------------------- #
    def create_event(self, payload: EventInput) -> Event:
        start = parse_iso(payload.starts_at)
        end = parse_iso(payload.ends_at) or (start + timedelta(hours=4) if start else None)
        if start and end and end <= start:
            # A create with a backwards range still gets a usable schedule.
            end = start + timedelta(hours=4)
        conn = self._connect()
        try:
            params = (
                payload.title,
                self._unique_slug(conn, payload.title),
                payload.category,
                pretty_date(start, end),
                pretty_time(start, end),
                payload.venue,
                payload.capacity,
                payload.speaker or "Prime IT Club",
                payload.description,
                payload.starts_at,
                end.isoformat() if end else None,
            )
            insert_sql = (
                "INSERT INTO events (title, slug, category, date, time, venue,\n"
                "                    capacity, registered_count, speaker,\n"
                "                    description, starts_at, ends_at)\n"
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)"
            )
            if self.db.dialect == "postgres":
                row = conn.execute(insert_sql + " RETURNING id", params).fetchone()
                event_id = int(row["id"])
            else:
                event_id = int(conn.execute(insert_sql, params).lastrowid)
            conn.commit()
        finally:
            conn.close()
        created = self.get_event(event_id)
        assert created is not None
        return created

    def update_event(self, event_id: int, patch: EventPatch) -> Optional[Event]:
        current = self.get_event(event_id)
        if not current:
            return None
        data = patch.model_dump(exclude_unset=True)
        if not data:
            return current

        starts_at = data.get("starts_at", current.starts_at)
        ends_at = data.get("ends_at", current.ends_at)
        start = parse_iso(starts_at)
        end = parse_iso(ends_at)

        # Keep a sane schedule. This covers both a PATCH that moves only the
        # start (no ends_at sent) and the admin form, which posts both fields,
        # leaving the old end behind when the start moved. Either way the
        # original duration is carried across instead of an end before a start.
        if start and (end is None or end <= start):
            old_start = parse_iso(current.starts_at)
            old_end = parse_iso(current.ends_at)
            duration = (old_end - old_start) if old_start and old_end else None
            if not duration or duration <= timedelta(0):
                duration = timedelta(hours=4)
            end = start + duration
            ends_at = end.isoformat()

        fields = {
            "title": data.get("title", current.title),
            "category": data.get("category", current.category),
            "venue": data.get("venue", current.venue),
            "capacity": data.get("capacity", current.capacity),
            "speaker": data.get("speaker", current.speaker),
            "description": data.get("description", current.description),
            "starts_at": starts_at,
            "ends_at": ends_at,
            "date": pretty_date(start, end),
            "time": pretty_time(start, end),
        }
        conn = self._connect()
        try:
            conn.execute(
                """UPDATE events SET title = ?, category = ?, venue = ?, capacity = ?,
                           speaker = ?, description = ?, starts_at = ?, ends_at = ?,
                           date = ?, time = ?
                   WHERE id = ?""",
                (
                    fields["title"],
                    fields["category"],
                    fields["venue"],
                    fields["capacity"],
                    fields["speaker"],
                    fields["description"],
                    fields["starts_at"],
                    fields["ends_at"],
                    fields["date"],
                    fields["time"],
                    event_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_event(event_id)

    def delete_event(self, event_id: int) -> Dict[str, int]:
        """Delete an event and its tickets. Returns how many rows went with it."""
        conn = self._connect()
        try:
            removed_tickets = conn.execute(
                "DELETE FROM tickets WHERE event_id = ?", (event_id,)
            ).rowcount
            removed_event = conn.execute(
                "DELETE FROM events WHERE id = ?", (event_id,)
            ).rowcount
            conn.commit()
            return {"events": removed_event, "tickets": removed_tickets}
        finally:
            conn.close()

    @staticmethod
    def _unique_slug(conn: Any, title: str) -> str:
        base = "".join(c if c.isalnum() else "-" for c in title.lower()).strip("-")
        base = "-".join(filter(None, base.split("-"))) or "event"
        slug = base
        counter = 2
        while conn.execute("SELECT 1 FROM events WHERE slug = ?", (slug,)).fetchone():
            slug = f"{base}-{counter}"
            counter += 1
        return slug

    # -- tickets export ----------------------------------------------------- #
    def all_tickets(self, event_id: Optional[int] = None) -> List[Ticket]:
        conn = self._connect()
        try:
            if event_id:
                rows = conn.execute(
                    "SELECT * FROM tickets WHERE event_id = ? "
                    "ORDER BY COALESCE(created_ms, created_at) DESC, token DESC",
                    (event_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tickets "
                    "ORDER BY COALESCE(created_ms, created_at) DESC, token DESC"
                ).fetchall()
            return [self._row_to_ticket(r) for r in rows]
        finally:
            conn.close()

    # -- diagnostics -------------------------------------------------------- #
    def health(self) -> Dict[str, Any]:
        """What the API is actually talking to, for /api/health.

        Reports the host and database name only - never the password - so the
        endpoint can stay public.
        """
        started = time.perf_counter()
        counts: Dict[str, int] = {}
        error = ""
        ok = False
        try:
            conn = self._connect()
            try:
                conn.execute("SELECT 1 AS ok").fetchone()
                counts["events"] = int(
                    conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
                )
                counts["tickets"] = int(
                    conn.execute("SELECT COUNT(*) AS c FROM tickets").fetchone()["c"]
                )
                ok = True
            finally:
                conn.close()
        except Exception as exc:
            error = str(exc)
        return {
            "ok": ok,
            "backend": self.db.dialect,
            "target": self.db.target,
            "durable": self.db.dialect == "postgres",
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "counts": counts,
            "error": error,
        }

    def selftest(self) -> Dict[str, Any]:
        """Write, read, check in and clean up a throwaway event.

        The admin console calls this after a deploy to prove the database is
        reachable and writable. It deletes everything it creates.
        """
        steps: List[Dict[str, Any]] = []
        holder: Dict[str, Any] = {"event_id": None, "token": None}
        started = time.perf_counter()

        def run(name: str, action) -> bool:
            mark = time.perf_counter()
            try:
                detail = action()
                steps.append(
                    {
                        "step": name,
                        "ok": True,
                        "detail": detail,
                        "ms": round((time.perf_counter() - mark) * 1000, 1),
                    }
                )
                return True
            except Exception as exc:
                steps.append(
                    {
                        "step": name,
                        "ok": False,
                        "detail": str(exc),
                        "ms": round((time.perf_counter() - mark) * 1000, 1),
                    }
                )
                return False

        try:
            start = _nepal_now() + timedelta(days=365)
            payload = EventInput(
                title="Prime ticket self-test",
                category="Diagnostic",
                venue="Prime College",
                capacity=3,
                speaker="Automated check",
                description="Created by /api/admin/selftest and deleted again.",
                starts_at=start.isoformat(),
            )

            def create() -> str:
                event = self.create_event(payload)
                holder["event_id"] = event.id
                return f"created event #{event.id}"

            def register() -> str:
                ticket, created = self.register_ticket(
                    RegistrationRequest(
                        event_id=holder["event_id"],
                        student_name="Self Test",
                        student_email="selftest@primeitclub.com",
                        college_id="QA-SELFTEST",
                    )
                )
                if ticket is None or not created:
                    raise RuntimeError("registration did not create a ticket")
                holder["token"] = ticket.token
                return f"issued pass {ticket.ticket_id}"

            def duplicate() -> str:
                ticket, created = self.register_ticket(
                    RegistrationRequest(
                        event_id=holder["event_id"],
                        student_name="Self Test",
                        student_email="selftest@primeitclub.com",
                        college_id="QA-SELFTEST",
                    )
                )
                if ticket is None or created:
                    raise RuntimeError("duplicate sign-up was not recognised")
                if ticket.token != holder["token"]:
                    raise RuntimeError("duplicate returned a different pass")
                return "duplicate sign-up returned the same pass"

            def check_in() -> str:
                result = self.check_in(holder["token"])
                if not result.get("success"):
                    raise RuntimeError(result.get("message", "check-in failed"))
                again = self.check_in(holder["token"])
                if again.get("success") or not again.get("already_checked"):
                    raise RuntimeError("a second scan was accepted")
                return "checked in once, second scan refused"

            def read_back() -> str:
                found = self.attendees(event_id=holder["event_id"], status="checked")
                if found["total"] != 1:
                    raise RuntimeError(f"expected 1 checked-in pass, found {found['total']}")
                return "attendee list agrees with the gate"

            ok = run("create event", create) and run("register pass", register)
            ok = ok and run("duplicate guard", duplicate)
            ok = ok and run("gate check-in", check_in)
            ok = ok and run("read back", read_back)
            return {
                "ok": ok and all(step["ok"] for step in steps),
                "backend": self.db.dialect,
                "target": self.db.target,
                "steps": steps,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        finally:
            if holder["event_id"] is not None:
                try:
                    self.delete_event(holder["event_id"])
                except Exception as exc:
                    steps.append(
                        {
                            "step": "cleanup",
                            "ok": False,
                            "detail": f"could not remove the test event: {exc}",
                            "ms": 0.0,
                        }
                    )
