# Taking the ticket system live

Everything that can be done from code is done and deployed. Three dashboard actions remain, and
none of them can be performed without your own Supabase / Render / Vercel login.

Live now:

| Piece | URL | State |
| --- | --- | --- |
| Public ticket site (Vercel, proxies to Render) | <https://prime-event-ticket-manager.vercel.app> | Working, **on ephemeral SQLite** |
| Ticket API (Render) | <https://prime-event-ticket-manager.onrender.com/api/health> | Version 2.1.0, no `DATABASE_URL`, no `ADMIN_TOKEN` |
| Admin console (Vercel) | <https://prime-ticket-admin.vercel.app> | **Not deployed** (`DEPLOYMENT_NOT_FOUND`) |

Until step 1 and 2 are done, passes are stored in a file on the Render instance: a redeploy or a
restart wipes the roster, and the admin console cannot sign in because no admin token exists.

---

## Step 1 - Point the API at your Supabase database

1. Open your Supabase project.
2. Go to **Project Settings -> Database -> Connection string** and pick the **URI** tab.
3. Turn on **Connection pooling** (or pick the **Transaction** / port `6543` host). Copy the string:

   ```text
   postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres
   ```

   Use the pooler, not `db.<project-ref>.supabase.co`: that direct host is IPv6-only and many
   networks cannot reach it.
4. If your database password contains `@`, `#`, `%`, `/` or `:`, URL-encode it (`@` becomes `%40`).
5. Optional but recommended: open **SQL Editor**, paste
   [sql/supabase_schema.sql](sql/supabase_schema.sql) and run it. The API creates the same tables
   on boot anyway; running it first lets you see them and turns on row level security. It is
   idempotent.

## Step 2 - Set the two environment variables on Render

Open **Render -> prime-event-ticket-manager -> Environment -> Add Environment Variable**.

| Key | Value |
| --- | --- |
| `DATABASE_URL` | the Supabase pooler URI from step 1 |
| `ADMIN_TOKEN` | a long random password, for example from `python -c "import secrets; print(secrets.token_urlsafe(24))"` |

Save. Render redeploys automatically. Keep the token private: anyone who has it can edit the
roster, and you will type it into the admin console to sign in.

## Step 3 - Prove it works

```bash
curl -s https://prime-event-ticket-manager.onrender.com/api/health
```

Expect, inside `storage`: `"backend":"postgres"`, `"durable":true`, and a `target` naming your
Supabase pooler host. If it still says `sqlite`, the variable is missing or misspelled.

Then open the public site, register for an event, reload, and use **My pass**. In Supabase's
**Table editor** you should see the row in `tickets`. Redeploy the Render service once and confirm
the pass is still there - that is the whole point of this change.

## Step 4 - Deploy the admin console to Vercel

The repository is public and already pushed:
<https://github.com/hola-mundo999/prime-ticket-admin>

1. Open <https://vercel.com/new> and import `prime-ticket-admin`.
2. Framework preset **Other**, build command **empty**, output directory **`.`**, root directory
   **default**. There is no build step; it is three static files.
3. No environment variables are needed. `vercel.json` already rewrites `/api/*` to the Render
   service, so the console is same-origin in production.
4. Deploy, open the URL, paste the `ADMIN_TOKEN` from step 2, leave **API base** empty.
5. Go to **Overview -> System status**: it should read **Supabase / Postgres - survives redeploys**
   with your host and a round-trip time.
6. Click **Run self-test**. It creates a throwaway event, issues a pass, rejects the duplicate,
   checks in at the gate, reads the roster back and deletes everything again. Every step must be
   green; nothing is left behind.

If the admin console shows "this API has no ADMIN_TOKEN configured", step 2 was not saved on
Render. If it rejects the token, the value differs by a character or whitespace.

---

## What was changed in code (nothing left to fix here)

- `db.py` picks SQLite or Postgres from `DATABASE_URL` and hides the dialect differences
  (placeholders, boolean columns, `RETURNING`, advisory locks, row locks, migrations).
- `models.py` store runs the same queries on both engines, creates the schema on boot, and seeds
  the Prime IT Club catalog; per-event counts are single round trips so a hosted database stays
  fast.
- `main.py` exposes storage details on `/api/health` and a guarded `POST /api/admin/selftest`.
- The admin console gained the System status panel and the self-test button, and now keeps an
  event's duration when its start time moves.
- 44 tests pass locally; the public site and admin console were exercised end to end in a browser
  (registration, duplicate, sold out, QR download, `.ics` export, gate accept / already-used /
  unknown token, event CRUD, attendee filters, CSV, delete-with-passes).

Limitations worth knowing:

- I could not run the Postgres code path against a real Postgres server from this machine (no
  network access to package indexes, no database driver installable). It is covered by adapter
  tests that assert the Postgres DDL, placeholder rewriting and locking, and the live environment
  proves it the moment `/api/health` says `postgres`.
- Supabase, Render and Vercel all need your account, so steps 1-4 are yours.
- Nothing new can be verified on the live database until steps 1-2 land.

---

## Copy-paste message for ChatGPT

```text
I have a deployed event ticketing system and I need step-by-step help finishing the
production setup. I can follow dashboard instructions; I cannot run SQL against a
database except through the Supabase SQL editor.

Repositories:
- API + public site: https://github.com/hola-mundo999/prime-event-ticket-manager
  (FastAPI, deployed on Render, static front end on Vercel with a rewrite to Render)
- Admin console: https://github.com/hola-mundo999/prime-event-ticket-admin
  (static HTML/CSS/JS for Vercel, calls the same API)

Current URLs:
- https://prime-event-ticket-manager.vercel.app
- https://prime-event-ticket-manager.onrender.com/api/health
- https://prime-ticket-admin.vercel.app (not deployed yet, Vercel says DEPLOYMENT_NOT_FOUND)

The API already supports Postgres: it reads DATABASE_URL (or POSTGRES_URL) and otherwise
falls back to a local SQLite file, which is what it is doing now. /api/health returns
{"storage":{"backend":"sqlite","durable":false,...}} and admin routes return 503 because
ADMIN_TOKEN is not set on Render. Tables are created automatically on boot; the schema is
in sql/supabase_schema.sql if it has to be run by hand.

What I need:
1. How to copy the right connection string from Supabase for a hosted API (the pooler URI,
   port 6543, not the direct db.<ref>.supabase.co host), and how to URL-encode the password.
2. How to set DATABASE_URL and ADMIN_TOKEN on a Render web service and confirm it redeployed
   with them.
3. How to verify from /api/health that it is really on Postgres, and how to tell that data
   survives a redeploy.
4. How to import the admin repo into Vercel as a static site (framework preset, build command,
   output directory) and sign in with the token.
5. What to check if it fails: wrong password, IPv6-only host, prepared statement errors on a
   pooler, CORS when the API base is set manually, and 401 vs 503 from the admin routes.

Please give exact click paths and the commands to verify each step, and tell me what to check
before moving to the next step.
```
