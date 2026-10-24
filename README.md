# Prime IT Club - Event & QR Ticket Manager

The registration desk for Prime IT Club's programmes: the ICT Meetup, Hackaverse, the Prime IT
Job Fair, the Shrijay Memorial ICT Cup and the club's workshop and panel series. Students claim a
QR pass in seconds, coordinators check them in at the door, and nobody is counted twice.

The public site wears the club's own identity, taken from
[primeitclub.com](https://primeitclub.com): royal blue `#123ABC`, warm orange `#EE8C3B`, soft
`#F5F7FB` surfaces, Poppins and DM Sans.

Developed by **[Samyog Sapkota](https://github.com/hola-mundo999)** for Prime IT Club.

---

## What is inside

**Public site** (`index.html`, served by FastAPI at `/`)

- Live event catalog with category filters, search, an upcoming/past switch and a status per event
  (open, live, sold out, finished).
- Event detail drawer with the full description, speaker, venue and seat count.
- Registration that issues a QR pass with a download, a copy-token action and an `.ics` calendar
  file. Registering twice returns the same pass instead of failing.
- Gate terminal for coordinators, with accepted, already-used and unknown-token states.
- "My pass" that survives a reload in the same browser.
- Live numbers: passes issued, checked in, attendance rate and per-event fill bars.

**API** (`main.py`, `models.py`, `db.py`, `qr_service.py`)

- FastAPI and Pydantic v2 over **SQLite locally and Postgres (Supabase) in production**. The same
  SQL runs on both; `db.py` picks the engine from `DATABASE_URL` and rewrites placeholders.
- Seat counts come from the issued tickets, so the roster and the counter cannot drift apart.
- Admin endpoints guarded by a shared token, powering the separate
  [admin console](https://github.com/hola-mundo999/prime-event-ticket-admin).
- `/api/health` reports which database is live and whether it is durable, and
  `/api/admin/selftest` performs a real write / read / check-in / cleanup round trip.

## The catalog

Seeded from the club's real programme history, plus the next edition of each recurring series so
the ticket flow can be exercised today.

| Programme | Kind | Editions |
| --- | --- | --- |
| ICT Meetup | Annual tech meetup | V6.0 (2019), V7.0 (2024), V8.0 (2026) |
| Hackaverse | Hackathon | Genesis (2024), Hack the Hassle (2025), 2026 edition |
| Prime IT Job Fair | Career fair | 2025 and the 2026 edition |
| Shrijay Memorial ICT Cup | Futsal tournament | 2025 |
| Workshops | Git and GitHub, pitch decks, AI and LLM, MS Office | 2024 to 2026 |
| Panels | IWD Conclave, Yathartha, career mentorship | 2024 to 2025 |

`CATALOG` in [models.py](models.py) is the single source of truth. On boot the store inserts any
missing event and adopts rows from the old three-event demo seed without losing their tickets.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Service check plus the storage backend and target |
| `GET` | `/api/events?scope=all` or `upcoming` | Catalog with `status` and `seats_left` |
| `POST` | `/api/register` | Issue a pass, returns the QR as a data URI |
| `POST` | `/api/check-in?token=` | Verify at the gate |
| `GET` | `/api/stats` | Public numbers |
| `GET` | `/api/export.csv` | Every ticket as CSV |
| `GET` | `/api/admin/session` | Token check |
| `GET` | `/api/admin/overview` | Totals, per-event rows, latest passes |
| `GET` `POST` `PATCH` `DELETE` | `/api/admin/events` and `/api/admin/events/{id}` | Event CRUD |
| `GET` | `/api/admin/attendees?event_id=&q=&status=` | Roster with filters |
| `GET` | `/api/admin/export.csv?event_id=` | Scoped export |
| `POST` | `/api/admin/selftest` | Writes, reads, checks in and deletes a throwaway event |

Admin routes need the `x-admin-token` header matching the `ADMIN_TOKEN` environment variable. With
no `ADMIN_TOKEN` set they answer `503`, which is deliberate.

Interactive docs live at `/docs` while the server runs.

## Run locally

```bash
python -m venv venv
venv\Scripts\activate            # Windows
pip install -r requirements.txt

ADMIN_TOKEN=dev-admin uvicorn main:app --reload --port 8000
```

Open <http://localhost:8000> for the site and `/docs` for the API. The SQLite file
`event_tickets.db` is created on first run and is gitignored. With no `DATABASE_URL` set, the app
never touches Postgres, so local work needs no database server.

## Store registrations in Supabase

1. Create a project at [supabase.com](https://supabase.com).
2. **Project Settings -> Database -> Connection string -> URI**, and copy the **Connection pooling**
   (transaction, port `6543`) URI. It contains the database password.
3. Set it on the API host as `DATABASE_URL`. Set `ADMIN_TOKEN` there too.
4. Restart the service. The API creates the tables and seeds the event catalog itself; no manual
   SQL is required. If your database user cannot run DDL, paste
   [sql/supabase_schema.sql](sql/supabase_schema.sql) into the Supabase SQL editor first (it is
   idempotent and also turns on row level security).
5. Check `GET /api/health`: `storage.backend` should read `postgres` and `storage.durable` `true`.
   In the admin console, **Overview -> Run self-test** proves a real write works.

The pass ordering column, boolean handling, advisory locks and statement placeholders are handled
in `db.py`, so the rest of the code is engine-agnostic. `DATABASE_URL` accepts `postgres://` or
`postgresql://` and appends `sslmode=require` when it is missing.

## Tests

```bash
pytest
```

44 tests cover the catalog and its schedule fields, registration validation, duplicate handling,
capacity limits, check-in behaviour, stats, full CSV export, admin authentication, event CRUD,
attendee filters, scoped exports, the health payload, the self-test round trip, and the Postgres
path (URL handling, placeholder rewriting, Postgres DDL, advisory-lock and `FOR UPDATE` writes).

## Deploy

**API and site on Render** (configured in `render.yaml`)

- Build: `pip install -r requirements.txt`
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Environment: `DATABASE_URL` (Supabase pooler URI) and `ADMIN_TOKEN`. Both are marked `sync: false`
  in the blueprint, so Render asks for them instead of reading them from the repository.
- With `DATABASE_URL` set, registrations live in Supabase and survive redeploys. Without it the app
  falls back to the instance's SQLite file, and `/api/health` says `sqlite` / `durable: false` so
  the fallback is visible instead of silent.

**Static site on Vercel** (`vercel.json`)

The repository ships `index.html` and `assets/`, and rewrites `/api/*` to the Render service, so
the Vercel URL is a full working site while the data stays in the API.

**Admin console**

Separate repository: [prime-event-ticket-admin](https://github.com/hola-mundo999/prime-event-ticket-admin).
Import it on Vercel as a static project with no build command; its `vercel.json` rewrites `/api/*`
to the Render service, so nothing else needs configuring. Sign in with the same `ADMIN_TOKEN`.

## License

MIT
