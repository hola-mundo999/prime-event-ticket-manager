-- Prime IT Club ticket desk - Supabase / Postgres schema
--
-- You do not have to run this by hand: the API creates and migrates the same
-- schema on startup. Run it once in Supabase -> SQL Editor if you prefer to
-- see the tables before the app starts, or if your database user cannot run
-- DDL. It is idempotent, so running it twice is harmless.

CREATE TABLE IF NOT EXISTS events (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    venue TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    registered_count INTEGER NOT NULL DEFAULT 0,
    speaker TEXT NOT NULL,
    description TEXT NOT NULL,
    slug TEXT,
    starts_at TEXT,
    ends_at TEXT
);

CREATE TABLE IF NOT EXISTS tickets (
    token TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    event_id INTEGER NOT NULL REFERENCES events(id),
    event_title TEXT NOT NULL,
    student_name TEXT NOT NULL,
    student_email TEXT NOT NULL,
    college_id TEXT NOT NULL,
    is_checked_in BOOLEAN NOT NULL DEFAULT FALSE,
    check_in_time TEXT,
    created_at TEXT NOT NULL,
    created_ms TEXT
);

CREATE INDEX IF NOT EXISTS idx_tickets_event ON tickets(event_id);
CREATE INDEX IF NOT EXISTS idx_tickets_email ON tickets(student_email);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_slug ON events(slug)
    WHERE slug IS NOT NULL AND slug <> '';

-- The app seeds the Prime IT Club programme catalog itself on boot.
--
-- Row level security: the API talks to Postgres directly with the database
-- password, so RLS does not apply to it. Enable RLS anyway so a leaked anon
-- key cannot read student names and emails from the browser:
ALTER TABLE events ENABLE ROW LEVEL SECURITY;
ALTER TABLE tickets ENABLE ROW LEVEL SECURITY;
-- No policies are created on purpose: only the service/database role (used by
-- the API) bypasses RLS. The anon and authenticated roles can read nothing.
