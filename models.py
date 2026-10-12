from pydantic import BaseModel, EmailStr
from typing import List, Optional, Dict
import time
import uuid


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
    student_name: str
    student_email: str
    college_id: str


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
    def __init__(self):
        self.events: Dict[int, Event] = {
            1: Event(
                id=1,
                title="AI & LLM Hands-on Workshop",
                category="Technical Workshop",
                date="Nov 15, 2024",
                time="10:00 AM - 1:00 PM",
                venue="Prime College Computer Lab 3",
                capacity=60,
                registered_count=18,
                speaker="Industry AI Lead",
                description="Practical deep-dive into prompt engineering, vector databases, and building full-stack AI agents using Python."
            ),
            2: Event(
                id=2,
                title="Git, GitHub & Open Source Bootcamp",
                category="Community Bootcamp",
                date="Nov 22, 2024",
                time="11:30 AM - 2:30 PM",
                venue="Prime IT Club Seminar Hall",
                capacity=80,
                registered_count=35,
                speaker="PR Director & Tech Team",
                description="Mastering branching strategies, pull requests, collaborative workflows, and preparing for Hackathons."
            ),
            3: Event(
                id=3,
                title="Prime CSIT Intra-College Hackathon",
                category="Hackathon",
                date="Dec 05, 2024",
                time="9:00 AM - 6:00 PM",
                venue="Prime College Main Auditorium",
                capacity=120,
                registered_count=52,
                speaker="Mentors & Judges Panel",
                description="A full-day hackathon building practical solutions for college automation, campus life, and student utilities."
            ),
        }
        self.tickets: Dict[str, Ticket] = {}

    def get_events(self) -> List[Event]:
        return list(self.events.values())

    def get_event(self, event_id: int) -> Optional[Event]:
        return self.events.get(event_id)

    def register_ticket(self, req: RegistrationRequest) -> Optional[Ticket]:
        event = self.get_event(req.event_id)
        if not event:
            return None

        event.registered_count += 1
        ticket_id = f"PRIME-{int(time.time()) % 100000:05d}"
        token = str(uuid.uuid4())[:8].upper()

        ticket = Ticket(
            ticket_id=ticket_id,
            token=token,
            event_id=event.id,
            event_title=event.title,
            student_name=req.student_name,
            student_email=req.student_email,
            college_id=req.college_id,
            created_at=time.strftime("%Y-%m-%d %H:%M:%S")
        )
        self.tickets[token] = ticket
        return ticket

    def check_in(self, token: str) -> Dict:
        clean_token = token.strip().upper()
        ticket = self.tickets.get(clean_token)
        if not ticket:
            return {"success": False, "message": "❌ Invalid Ticket Token. Record not found."}

        if ticket.is_checked_in:
            return {
                "success": False,
                "already_checked": True,
                "message": f"⚠️ Ticket was already checked in at {ticket.check_in_time}!",
                "ticket": ticket
            }

        ticket.is_checked_in = True
        ticket.check_in_time = time.strftime("%H:%M:%S")
        return {
            "success": True,
            "message": f"✅ Verified! Welcome, {ticket.student_name}.",
            "ticket": ticket
        }
