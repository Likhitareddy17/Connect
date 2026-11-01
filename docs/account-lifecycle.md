# Account lifecycle: email verification, password reset, account deletion

## Configuration (`.env`)

```env
SMTP_HOST=smtp.example.com
SMTP_PORT=587            # 587 = STARTTLS (required), 465 = implicit TLS
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_FROM_EMAIL=noreply@example.com
PUBLIC_BASE_URL=https://connect.example.com   # origin used in e-mailed links; use https in production
```

* Without `SMTP_HOST` + a from-address, registration still creates the account but shows
  "we couldn't send the email" with a resend button; nothing is logged except a generic error line.
* The sender never falls back to plain-text SMTP: on non-465 ports the server must support STARTTLS.
* If `PUBLIC_BASE_URL` is empty, links use the address the browser used (fine for localhost, **not** for
  production behind a proxy: set it).
* Local testing: use a real SMTP account that supports STARTTLS (Gmail with an app password:
  `smtp.gmail.com`, port 587; or a Mailtrap / Brevo inbox). Tests never send mail: `smtplib` is replaced in `tests/conftest.py`.

## Database

`python init_db.py` is idempotent. It creates `email_verification_tokens` and `password_reset_tokens`
and adds `users.email_verified`. **Existing users are migrated as verified** (the upgrade default is TRUE);
fresh installs default to FALSE and the app always sets the flag explicitly on registration.
Run it **before** deploying the new code: a `users` row without the column is treated as unverified.

## Behaviour

| Flow | Details |
|---|---|
| Register | Creates an unverified user, mails a 24 h single-use link, does **not** log in. |
| Verify | `GET /verify-email?token=...` only shows a button; `POST` spends the token (mail scanners cannot burn it). Does not log in. |
| Login | Correct password but unverified: 403 + message + resend button. Wrong password: the normal generic error. |
| Resend | `POST /resend-verification`. Same response for unknown / verified / unverified addresses. 5/h per IP and 3/h per address. |
| Forgot password | `POST /forgot-password`. Same response whether or not the account exists. 1 h single-use link; a newer link voids older ones. Same rate limits. |
| Reset | `GET /reset-password?token=...` shows the form; `POST` sets the password, marks the address verified, voids all other reset links, and does **not** log in. |
| Sessions | Session and remember-me cookies embed an HMAC of the password hash, so a password change signs out every device. **Deploying this change signs everyone out once** (old cookies have no fingerprint). |
| Delete | `GET/POST /account/delete` (login required, CSRF, 5/h): current password + typing `DELETE`. Deletes the signed-in user only; foreign keys cascade to mood check-ins, chat sessions + messages, journal entries, jaap sessions and both token tables. Then logs out and clears the session. |

Tokens are 256-bit (`secrets.token_urlsafe(32)`), only their SHA-256 is stored, expiry is computed by MySQL in UTC, and a token is
spent with one atomic `UPDATE ... WHERE used_at IS NULL AND expires_at > UTC_TIMESTAMP()` so concurrent use succeeds once.

## Known limitations

* `/register` still says "an account with that email already exists" (existing behaviour, rate limited to 10/h). Hiding it would need
  a different sign-up flow.
* Rate limits are per process unless `LIMITER_STORAGE_URI` points at Redis.
* Expired/used token rows are kept (no cleanup job); they are tiny and cascade-deleted with the user.
* Account deletion is immediate and irreversible; there is no grace period or data export.
* No confirmation e-mail is sent after a password change or deletion.
