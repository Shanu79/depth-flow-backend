# DepthFlow Backend

FastAPI + SQLAlchemy backend for **DepthFlow AI** (depthflow.io). It turns a still image into a 3D "depth parallax" video and serves the React web app, the Android app (`com.shin.depthflow`) and developers using API keys.

Highlights: email/OTP + Google sign-in, credit ledger, subscriptions (Dodo Payments on web, Google Play Billing on Android), watermarking for free users, the **Happy Hour token** feature (60 minutes of unlimited generation for Monthly/Yearly subscribers) and its **notifier** (website bell + Android push + admin broadcast).

- Client contract for Happy Hour and notifications (web, Android, admin panel): [docs/HAPPY_HOUR_FRONTEND_API.md](docs/HAPPY_HOUR_FRONTEND_API.md)
- Android app integration guide (self-contained, meant to be fed to the Claude instance in the Android project): [docs/ANDROID_INTEGRATION.md](docs/ANDROID_INTEGRATION.md)
- Architecture notes, business rules and known issues: [CLAUDE.md](CLAUDE.md)
- Interactive API docs when the server is running: `http://localhost:8000/docs`

## Requirements
- Python 3.10+
- ffmpeg is bundled through `imageio-ffmpeg` (used for the free-tier watermark)
- SQLite for local development, PostgreSQL in production

## Quick start (Windows / PowerShell)

```powershell
git clone <repo-url> depth-flow-backend
cd depth-flow-backend

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt     # file is UTF-16; if pip complains, re-save it as UTF-8

# create a .env file in the project root (variables listed below)
alembic upgrade head                # create/upgrade the database schema
python -m uvicorn main:app --reload --port 8000
```

The API is now at `http://localhost:8000`.

## Configuration (`.env`, never commit it)

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Default `sqlite:///./depthflow.db`. `postgres://` URLs are rewritten to `postgresql://` |
| `APP_SECRET_KEY` | JWT signing key (set a strong value; the built-in default is insecure) |
| `ZEPTOMAIL_SEND_TOKEN`, `ZEPTOMAIL_FROM_ADDRESS`, `ZEPTOMAIL_FROM_NAME` | OTP / password-reset email |
| `IMMERSITY_CLIENT_ID`, `IMMERSITY_CLIENT_SECRET` | Immersity generation API (`/ai/generate-3d`) |
| `DEPTHFLOW_ENGINE_URL`, `DEPTHFLOW_SECRET_KEY` | Own render engine (`/ai/depthflow/generate-3d`), e.g. `http://localhost:8001` |
| `DEPTHFLOW_API_COST` | Optional base credit cost per DepthFlow generation (default 20) |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI` | Web Google sign-in |
| `GOOGLE_PRIVATE_KEY`, `GOOGLE_PRIVATE_KEY_ID`, `GOOGLE_PROJECT_ID`, `GOOGLE_CLIENT_EMAIL`, `GOOGLE_CLIENT_CERT_URL` | Google Play service account (or a local `google-play-service-account.json`) |
| `DODO_PAYMENTS_API_KEY`, `DODO_WEBHOOK_SECRET` | Dodo Payments |
| `DODO_ENVIRONMENT` | `live_mode` (default) or `test_mode`. **Use `test_mode` locally** so you never touch live payments |
| `FRONTEND_URL`, `BASE_URL` | Web app URL (redirects) and this API's public URL (used to build video links) |
| `FIREBASE_PROJECT_ID`, `FIREBASE_SERVICE_ACCOUNT_FILE` (or `FIREBASE_SERVICE_ACCOUNT_JSON`) | Android push via FCM. Optional: unset = push disabled, the in-app inbox still works |
| `FCM_DRY_RUN` | `true` logs push payloads instead of sending (local dev) |
| `NOTIFY_SWEEP_ENABLED`, `NOTIFY_SWEEP_SECONDS` | Server loop for "ending soon"/"ended"/new-month notices (default on, every 60 s) |

## Database & migrations
- Tables are also created on startup (`Base.metadata.create_all`), but **schema changes go through Alembic**:
  ```powershell
  alembic revision --autogenerate -m "describe change"
  alembic upgrade head
  ```
- Latest revision: `e9b4c7a2d815` (notifications; builds on Happy Hour v2 `d3f8b2a61c47`). `alembic downgrade -1` steps back one.
- Back up before migrating a database you care about (for SQLite just copy `depthflow.db`).

## Tests

```powershell
python tests/test_happy_hour.py        # 22 Happy Hour tests; temp SQLite DBs, never touches your database
python tests/test_notifications.py     # 10 notifier tests (inbox, devices, triggers, sweep, FCM payloads, broadcast)
```
Output is one `PASS`/`FAIL` line per test and a final `N/N passed` (non-zero exit on failure). The Happy Hour tests cover allocation rules, activation, the one-session/one-activation-per-month rules, the 3600 s expiry, the admin video cap and concurrency, credit bypass and fallback, billing webhooks, and cancellation. The notifier tests stub FCM, so nothing is sent.

Manual helpers in `tests/`: `seed_happy_hour_users.py`, `seed_test_user.py`, `reset_tester.py`, `test_payments.py` (sends signed mock Dodo webhooks to a running local server).

## Local Happy Hour test users

```powershell
python tests/seed_happy_hour_users.py   # creates/resets the users below (local SQLite only)
```

| Email (password `Test@12345`) | Plan | Happy Hour tokens | Credits |
|---|---|---|---|
| `hh_monthly@example.com` | Pro monthly | 1 | 100 |
| `hh_yearly@example.com` | Pro yearly | 4 (1 activation / calendar month) | 100 |
| `hh_nocredits@example.com` | Pro monthly | 1 | 0 |
| `hh_free@example.com` | Free | none | 20 |
| `hh_admin@example.com` | Free, admin | none | 20 |

## Project structure

```
main.py                    app, CORS, static mount, router registration, Happy Hour error handler
database.py                engine / session / Base
models.py                  User, GenerationHistory, HappyHourEntitlement, HappyHourSession, AppSetting,
                           Notification, DeviceToken, NotificationBroadcast
schemas.py                 Pydantic schemas
auth.py                    JWT + API-key auth, password hashing
happy_hour.py              Happy Hour service (entitlements, activation, generation gate, admin setting)
notifications.py           notifier service (inbox, device tokens, sweep, admin broadcast)
push_fcm.py                Firebase Cloud Messaging sender (HTTP v1)
routers/
  auth_router.py           /auth/*        register, OTP, login, Google, profile, password reset
  ai_router.py             /ai/*          Immersity generation + history
  depthflow_ai_router.py   /ai/depthflow/* own engine generation, logs, billing
  payments_router.py       /payments/*    Dodo checkout + webhooks, Google Play verify + RTDN
  happy_hour_router.py     /happy-hour/*  status, activate, session, history
  notifications_router.py  /notifications/* inbox + Android device registration
  admin_router.py          /admin/*       users, Happy Hour settings, notification broadcasts
  contact_router.py        /contact
alembic/                   migrations
docs/                      client-facing API documentation
tests/                     test + seed scripts
cron_monthly_credits.py    monthly credit drip for yearly plans (run daily by an external scheduler)
cron_happy_hour_notify.py  one-shot notification sweep (optional: the API already runs it every 60 s)
backfill_keys.py, backup_db.py
```

## API overview

| Area | Main endpoints |
|---|---|
| Auth | `POST /auth/register`, `/auth/verify-otp`, `/auth/login`, `/auth/google/verify`, `GET /auth/me`, `/auth/forgot-password`, `/auth/reset-password` |
| Generation | `POST /ai/generate-3d`, `POST /ai/depthflow/generate-3d`, `GET /ai/history`, `GET /ai/depthflow/billing` |
| Payments | `POST /payments/create-checkout-session`, `/payments/cancel-subscription`, `/payments/sync-subscription`, `/payments/verify-purchase` (Google Play), webhooks `/payments/webhook`, `/payments/google-play-webhook` |
| Happy Hour | `GET /happy-hour/status`, `POST /happy-hour/activate`, `GET /happy-hour/session`, `GET /happy-hour/history` |
| Notifications | `GET /notifications`, `GET /notifications/unread-count`, `POST /notifications/{id}/read`, `POST /notifications/read-all`, `POST /notifications/devices`, `POST /notifications/devices/unregister` |
| Admin | `GET/PUT /admin/settings/happy-hour`, `POST /admin/notifications/happy-hour-broadcast`, `GET /admin/notifications/broadcasts`, `GET /admin/users`, `PUT/DELETE /admin/users/{id}` |

Authenticate with `Authorization: Bearer <jwt>` or `X-API-Key: <key>`.

### Happy Hour in short
- **Yearly** subscription: 4 tokens per year, carry forward, at most one activation per UTC calendar month. **Monthly**: 1 token per billing period, no rollover, re-granted on renewal. Workspace plans only.
- Activating a token (`Idempotency-Key` header) starts exactly 3600 s of unlimited generation. The expiry is stored server-side, so refresh, logout or closing the app never resets it.
- Each session has a video cap set by an admin (`PUT /admin/settings/happy-hour`, snapshotted per session). When it is reached, normal credit rules resume.
- Cancelling or expiring the subscription voids unused tokens and ends a running session.
- Errors look like `{"detail": "...", "code": "NO_TOKENS"}`.

### Notifications in short
- Every notice goes to the user's **inbox** first (website bell + Android list); Android devices also get an **FCM push**. Events: token ready, Happy Hour live, ending soon (about 10 min before), ended (time / video cap / cancelled), yearly "new month, activation open", and admin broadcasts. Each event notifies a user at most once.
- Website: poll `GET /notifications/unread-count` (30-60 s and on tab focus). Android: register the FCM token with `POST /notifications/devices`, unregister on logout; notification channel `happy_hour`.
- Admin: `POST /admin/notifications/happy-hour-broadcast` (`audience`: `eligible` or `all_subscribers`, `dry_run` to preview reach).
- Real push needs a Firebase project + service account in `.env`; without it push is off and the inbox still works. Local default: `FCM_DRY_RUN=true`.

## Scheduled jobs
- `python cron_monthly_credits.py`: run daily; credits yearly subscribers' monthly share.
- Happy Hour tokens and sessions need **no** scheduled job (expiry and monthly rules are evaluated on every request).
- Notification sweep ("ending soon" / "ended" / new-month notices) runs **inside the API process every 60 s**. If your host puts idle web processes to sleep, also schedule `python cron_happy_hour_notify.py` every minute (safe to overlap).

## Troubleshooting
- **Import errors**: activate the venv and re-run `pip install -r requirements.txt`.
- **`no such table` / missing column**: run `alembic upgrade head`.
- **Generation returns 500 locally**: the DepthFlow engine (`DEPTHFLOW_ENGINE_URL`) or Immersity credentials are not available. Happy Hour gating still works; a failed render does not use up a video slot or credits.
- **No push notifications**: push needs `FIREBASE_PROJECT_ID` + a service account and a registered device token. With `FCM_DRY_RUN=true` the server log shows `[FCM dry-run]` lines instead. The in-app inbox works regardless.
- **"Ending soon" / "ended" notices never arrive**: the sweep runs inside the API process (`NOTIFY_SWEEP_ENABLED`); if the host sleeps idle processes, schedule `cron_happy_hour_notify.py`.
- **Webhook returns "Invalid signature"**: check `DODO_WEBHOOK_SECRET` matches the secret you sign with.
- **Dev server fails to start**: check the console for missing env variables or database errors.

## Contributing
Keep changes focused, add tests where appropriate, and update `CLAUDE.md` and `docs/` when behaviour changes.
