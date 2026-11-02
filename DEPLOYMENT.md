# Deployment notes

Use these instructions to configure or verify the deployed services. They do not change production
settings.

## API environment

Before deploying the MFA-enabled API, open the `prime-ticket-api-samyog-2026` service in the Render
dashboard and select **Environment**. Add the following variables (or confirm existing values).
The `sync: false` entries in `render.yaml` intentionally have no values in the repository; enter
their values directly in Render:

| Variable | Required value |
| --- | --- |
| `DATABASE_URL` | Existing persistent Postgres connection string, if used |
| `ADMIN_EMAIL` | `samyog.sapkota1@gmail.com` (the only authorized administrator) |
| `ADMIN_PASSWORD` | The existing admin account password, or a new bootstrap password |
| `ADMIN_TOTP_ENCRYPTION_KEY` | A persistent Fernet key generated as described below |

Customer account support is separately gated and is not enabled or migrated by this deployment
manifest. Resend credentials and the customer frontend URL must not be added until a production
migration and activation are separately approved. The local implementation expects
`CUSTOMER_ACCOUNTS_ENABLED=true`, `RESEND_API_KEY`, `RESEND_FROM_EMAIL`, and a fixed
`CUSTOMER_WEB_URL`; no values for these were added to hosting configuration.

`ADMIN_EMAIL` must match the authorized address above. For initial account creation,
`ADMIN_PASSWORD` must be at least 10 characters and contain a letter and a number; common
passwords are rejected. The password is only used to create the account if it does not already
exist, so changing this Render variable does not reset an existing account's password.

Generate a Fernet key once on a trusted machine with the API dependency installed:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

The command outputs a URL-safe Base64 Fernet key (32 decoded bytes). In Render, add that output as
the value of `ADMIN_TOTP_ENCRYPTION_KEY`. Treat it as a secret: do not commit it, put it in this
repository, or print it in logs. Keep a secure backup and reuse the exact same key across process
restarts and deployments. The database contains an encrypted TOTP secret that cannot be decrypted
without this key; replacing it can make existing authenticator enrollment unusable.

After adding or updating the values, save the Render environment settings and redeploy/restart the
service. Do not change `ADMIN_TOTP_ENCRYPTION_KEY` for routine deployments.

Only the allowlisted admin account can sign in; account invitations and management are disabled.

## First Google Authenticator enrollment

After the API and static console are deployed:

1. Sign in as `samyog.sapkota1@gmail.com` with the account password.
2. Scan the displayed QR code in Google Authenticator. If needed, enter the manual setup key.
3. Type the current six-digit code to activate enrollment.
4. Save the ten recovery codes in a private password manager or another secure location separate
   from the device. Each is single-use; the codes cannot be displayed again.

Subsequent sign-ins require the password and a current TOTP code or unused recovery code. The API
verifies factors server-side. It stores the TOTP secret encrypted and recovery-code hashes only;
after enrollment neither secret is returned. Full sessions use HttpOnly, SameSite cookies, with
CSRF checks on state-changing admin requests. The session cookie is scoped to `/api` so the gate
check-in route can use the same authenticated session. Gate check-in now requires an admin session
and CSRF validation; the public-site gate form is not an authenticated scanner and cannot check in
tickets. Use the signed-in admin scanner.

The API applies the existing admin schema changes, including the admin profile preferences
column, on startup; no manual schema update is needed when the database role can run DDL. The
customer-account schema is an exception: it is not applied on startup and requires a separately
approved, operator-managed migration. For local SQLite only, the migration utility requires an
explicit file path and refuses to run when any database URL variable is present. The first MFA
schema migration invalidates existing admin sessions. Old bearer tokens and password-only
sessions cannot access admin routes.

## Static admin console

The console is a static site: use framework preset **Other**, no build command, and output
directory `.`. Its existing `vercel.json` rewrite sends `/api/*` to the ticket API, so no console
environment variables are required.

## Verification

- Check `GET /api/health` for minimal service availability. Detailed storage status is available
  only to an authenticated owner at `/api/admin/health`.
- Sign in with the allowlisted account, complete TOTP enrollment, and use an authenticator code
  for the next sign-in.
- Confirm an unused recovery code signs in once and fails if reused.
- Use the console's protected admin views and CSV export; public event registration and check-in
  should continue to work without an admin session.
