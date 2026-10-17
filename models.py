from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import List, Optional, Dict
import time
import uuid
import sqlite3
import os


DB_PATH = os.getenv("TICKET_DB_PATH", "event_tickets.db")


class Event(BaseModel):
    id: int
    title: str
    category: str
    date: str
    time: str
    venue: str
    capacity: int
    registered_count: int = 0
    speaker: str
    description: str


class RegistrationRequest(BaseModel):
    event_id: int
    student_name: str = Field(min_length=1, max_length=120)
    student_email: EmailStr
    college_id: str = Field(min_length=1, max_length=40)

    @field_validator("student_name", "college_id")
    @classmethod
    def strip_values(cls, v: str) -> str:
        return v.strip()


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


class DataStore:
    """SQLite-backed event and ticket store."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()
        self._seed_events_if_empty()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
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
                    description TEXT NOT NULL
                );
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
                    FOREIGN KEY (event_id) REFERENCES events(id)
                );
                CREATE INDEX IF NOT EXISTS idx_tickets_event ON tickets(event_id);
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _row_to_event(self, row: sqlite3.Row) -> Event:
        return Event(
            id=row["id"],
            title=row["title"],
            category=row["category"],
            date=row["date"],
            time=row["time"],
            venue=row["venue"],
            capacity=row["capacity"],
            registered_count=row["registered_count"],
            speaker=row["speaker"],
            description=row["description"],
        )

    def _row_to_ticket(self, row: sqlite3.Row) -> Ticket:
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

    def _seed_events_if_empty(self) -> None:
        conn = self._connect()
        try:
            count = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
            if count == 0:
                now = time.strftime("%Y-%m-%d %H:%M:%S")
                events = [
                    (
                        1,
                        "AI & LLM Hands-on Workshop",
                        "Technical Workshop",
                        "Nov 15, 2024",
                        "10:00 AM - 1:00 PM",
                        "Prime College Computer Lab 3",
                        60,
                        18,
                        "Industry AI Lead",
                        "Practical deep-dive into prompt engineering, vector databases, and building full-stack AI agents using Python.",
                    ),
                    (
                        2,
                        "Git, GitHub & Open Source Bootcamp",
                        "Community Bootcamp",
                        "Nov 22, 2024",
                        "11:30 AM - 2:30 PM",
                        "Prime IT Club Seminar Hall",
                        80,
                        35,
                        "PR Director & Tech Team",
                        "Mastering branching strategies, pull requests, collaborative workflows, and preparing for Hackathons.",
                    ),
                    (
                        3,
                        "Prime CSIT Intra-College Hackathon",
                        "Hackathon",
                        "Dec 05, 2024",
                        "9:00 AM - 6:00 PM",
                        "Prime College Main Auditorium",
                        120,
                        52,
                        "Mentors & Judges Panel",
                        "A full-day hackathon building practical solutions for college automation, campus life, and student utilities.",
                    ),
                ]
                conn.executemany(
                    """INSERT INTO events (id, title, category, date, time, venue,
                                             capacity, registered_count, speaker, description)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    events,
                )
                conn.commit()
        finally:
            conn.close()

    def get_events(self) -> List[Event]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY id"
            ).fetchall()
            return [self._row_to_event(r) for r in rows]
        finally:
            conn.close()

    def get_event(self, event_id: int) -> Optional[Event]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            return self._row_to_event(row) if row else None
        finally:
            conn.close()

    def _event_has_capacity(self, conn: sqlite3.Connection, event_id: int) -> bool:
        row = conn.execute(
            "SELECT capacity, registered_count FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
        if not row:
            return False
        return row["registered_count"] < row["capacity"]

    def register_ticket(self, req: RegistrationRequest) -> Optional[Ticket]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            event = conn.execute(
                "SELECT * FROM events WHERE id = ?", (req.event_id,)
            ).fetchone()
            if not event:
                conn.rollback()
                return None

            # Duplicate registration: same email + college_id for the same event
            existing = conn.execute(
                """SELECT token FROM tickets
                   WHERE event_id = ? AND student_email = ? AND college_id = ?""",
                (req.event_id, req.student_email, req.college_id),
            ).fetchone()
            if existing:
                conn.rollback()
                return Ticket(
                    ticket_id=conn.execute(
                        "SELECT ticket_id FROM tickets WHERE token = ?", (existing["token"],)
                    ).fetchone()["ticket_id"],
                    token=existing["token"],
                    event_id=event["id"],
                    event_title=event["title"],
                    student_name=req.student_name,
                    student_email=req.student_email,
                    college_id=req.college_id,
                    is_checked_in=bool(conn.execute(
                        "SELECT is_checked_in FROM tickets WHERE token = ?", (existing["token"],)
                    ).fetchone()["is_checked_in"]),
                    check_in_time=conn.execute(
                        "SELECT check_in_time FROM tickets WHERE token = ?", (existing["token"],)
                    ).fetchone()["check_in_time"],
                    created_at=conn.execute(
                        "SELECT created_at FROM tickets WHERE token = ?", (existing["token"],)
                    ).fetchone()["created_at"],
                )

            if not self._event_has_capacity(conn, event["id"]):
                conn.rollback()
                return None

            ticket_id = f"PRIME-{int(time.time()*1000) % 1000000:06d}"
            token = str(uuid.uuid4())[:8].upper()
            created_at = time.strftime("%Y-%m-%d %H:%M:%S")

            conn.execute(
                """INSERT INTO tickets (token, ticket_id, event_id, event_title,
                                        student_name, student_email, college_id,
                                        is_checked_in, check_in_time, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)""",
                (token, ticket_id, event["id"], event["title"],
                 req.student_name, req.student_email, req.college_id, created_at),
            )
            conn.execute(
                "UPDATE events SET registered_count = registered_count + 1 WHERE id = ?",
                (event["id"],),
            )
            conn.commit()
            return Ticket(
                ticket_id=ticket_id,
                token=token,
                event_id=event["id"],
                event_title=event["title"],
                student_name=req.student_name,
                student_email=req.student_email,
                college_id=req.college_id,
                is_checked_in=False,
                check_in_time=None,
                created_at=created_at,
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            return None
        finally:
            conn.close()

    def check_in(self, token: str) -> Dict:
        clean_token = token.strip().upper()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM tickets WHERE token = ?", (clean_token,)
            ).fetchone()
            if not row:
                conn.rollback()
                return {"success": False, "message": "❌ Invalid Ticket Token. Record not found."}

            if bool(row["is_checked_in"]):
                conn.rollback()
                return {
                    "success": False,
                    "already_checked": True,
                    "message": f"⚠️ Ticket was already checked in at {row['check_in_time']}!",
                    "ticket": self._row_to_ticket(row),
                }

            check_in_time = time.strftime("%H:%M:%S")
            conn.execute(
                "UPDATE tickets SET is_checked_in = 1, check_in_time = ? WHERE token = ?",
                (check_in_time, clean_token),
            )
            conn.commit()
            ticket = self._row_to_ticket(row)
            ticket.is_checked_in = True
            ticket.check_in_time = check_in_time
            return {
                "success": True,
                "message": f"✅ Verified! Welcome, {ticket.student_name}.",
                "ticket": ticket,
            }
        finally:
            conn.close()

    def get_stats(self) -> Dict:
        conn = self._connect()
        try:
            total_registered = conn.execute("SELECT COUNT(*) AS c FROM tickets").fetchone()["c"]
            total_checked_in = conn.execute(
                "SELECT COUNT(*) AS c FROM tickets WHERE is_checked_in = 1"
            ).fetchone()["c"]
            rows = conn.execute(
                "SELECT * FROM tickets ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
            recent_tickets = [self._row_to_ticket(r) for r in rows]
            return {
                "total_registered": total_registered,
                "total_checked_in": total_checked_in,
                "recent_tickets": recent_tickets,
            }
        finally:
            conn.close()
