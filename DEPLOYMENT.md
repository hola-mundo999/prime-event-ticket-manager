# Deployment notes

These are operator steps for a future release. No production settings or deployments were changed
as part of implementing MFA.

## API environment

Before deploying the MFA-enabled API, open the API host's secret environment settings and verify:

| Variable | Required value |
| --- | --- |
| `DATABASE_URL` | Existing persistent Postgres connection string, if used |
| `ADMIN_EMAIL` | `samyog.sapkota1@gmail.com` |
| `ADMIN_PASSWORD` | Existing strong password for the allowlisted account |
| `ADMIN_TOTP_ENCRYPTION_KEY` | Persistent Fernet key; do not rotate casually |

Generate a new key once, on a trusted machine with the API dependency installed:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Add it directly to the host's secret manager. Do not commit the value, put it in this repository,
or print it in logs. Keep a secure backup and reuse the exact same key across process restarts and
deployments. The database contains an encrypted TOTP secret that cannot be decrypted without this
key.

The code does not add or modify host environment settings, Render blueprints, Vercel projects, or
database-provider configuration. Confirm the existing admin database account uses the allowlisted
email before deploying. Only that account can sign in; account invitations and management are
disabled.

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
CSRF checks on state-changing admin requests.

The first MFA schema migration invalidates existing admin sessions. Old bearer tokens and
password-only sessions cannot access admin routes. Public event, registration, and check-in flows
remain separate from admin session authorization.

## Static admin console

The console is a static site: use framework preset **Other**, no build command, and output
directory `.`. Its existing `vercel.json` rewrite sends `/api/*` to the ticket API, so no console
environment variables are required.

## Verification

- Check `GET /api/health` for API/database status.
- Sign in with the allowlisted account, complete TOTP enrollment, and use an authenticator code
  for the next sign-in.
- Confirm an unused recovery code signs in once and fails if reused.
- Use the console's protected admin views and CSV export; public event registration and check-in
  should continue to work without an admin session.
