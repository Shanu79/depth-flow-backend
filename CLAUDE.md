# CLAUDE.md — DepthFlow Backend

FastAPI + SQLAlchemy backend for **DepthFlow AI** (depthflow.io): turns a still image into a 3D "depth parallax" video. Used by a React web app, an Android app (`com.shin.depthflow`), and developers via API key. Monetised with credits + subscriptions (Dodo Payments on web, Google Play Billing on Android).

## Run / dev commands (Windows, PowerShell)
```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt        # NOTE: requirements.txt is UTF-16 encoded (pip may need re-encoding to UTF-8)
python -m uvicorn main:app --reload    # http://127.0.0.1:8000 , Swagger at /docs
alembic upgrade head                   # migrations (env.py reads DATABASE_URL via database.py)
python tests/test_happy_hour.py        # 22 Happy Hour tests (temp SQLite DBs, never touches your DB)
python tests/test_notifications.py     # 10 notifier tests (FCM stubbed)
python cron_happy_hour_notify.py       # one-shot notification sweep (the API already runs it every 60 s)
python tests/seed_happy_hour_users.py  # local test users + entitlements (refuses non-SQLite DBs)
python cron_monthly_credits.py         # monthly credit drop job (run daily by external scheduler)
python backfill_keys.py                # give api_key to users lacking one
python backup_db.py                    # dumps users to prod_users_backup.json (gitignored via *backup*.json)
```
- No pytest (not installed): `tests/test_happy_hour.py` and `tests/test_notifications.py` are plain-assert runners (exit non-zero on failure; the second imports helpers from the first). The other `tests/` files are manual scripts: `seed_test_user.py` (creates dummy_tester@example.com + prints JWT), `reset_tester.py`, `test_payments.py` (signs mock Dodo webhooks with `standardwebhooks` and POSTs to localhost; JWT + webhook secret are hardcoded inside it).
- Default DB: `sqlite:///./depthflow.db`; prod uses Postgres (`postgres://` is rewritten to `postgresql://`). `psycopg2-binary` installed.
- Python 3.10+. requirements.txt is a full `pip freeze` that also includes unused Flask stuff.

## Layout
| File | Role |
|---|---|
| `main.py` | App, CORS `*`, mounts `/static`, `Base.metadata.create_all` on startup, includes routers, legacy alias `POST /ai/depthflow/verify-purchase` → payments `verify_google_play_purchase` (old Android clients) |
| `database.py` | engine, `SessionLocal`, `Base`, `get_db` |
| `models.py` | `User`, `GenerationHistory`, `HappyHourEntitlement`, `HappyHourSession`, `AppSetting`, `Notification`, `DeviceToken`, `NotificationBroadcast` |
| `schemas.py` | Pydantic: UserLogin/Create/Update/Response, Token, OTPVerify |
| `auth.py` | JWT (HS256, 7-day, secret `APP_SECRET_KEY`), argon2 hashing, `get_current_user` (hybrid: `X-API-Key` header first, else Bearer JWT) |
| `routers/auth_router.py` | `/auth/*` |
| `routers/ai_router.py` | `/ai/*` — Immersity-powered generation (3 styles) + `/ai/history` |
| `routers/depthflow_ai_router.py` | `/ai/depthflow/*` — own DepthFlow engine (separate service) |
| `routers/payments_router.py` | `/payments/*` — Dodo + Google Play |
| `routers/admin_router.py` | `/admin/users` list/update/delete + `/admin/settings/happy-hour` (requires `is_admin`) |
| `routers/contact_router.py` | `POST /contact` — only logs (email forwarding is a TODO) |
| `cron_monthly_credits.py` | yearly-plan monthly credit drip |
| `notifications.py` | notifier service: inbox, device tokens, event helpers, sweep, admin broadcast |
| `push_fcm.py` | FCM HTTP v1 sender (google-auth token, dry-run / disabled modes) |
| `routers/notifications_router.py` | `/notifications/*` inbox + device registration |
| `cron_happy_hour_notify.py` | one-shot notification sweep for an external scheduler |
| `happy_hour.py` | Happy Hour service: entitlements, activation, generation gate, admin setting (see section below) |
| `routers/happy_hour_router.py` | `/happy-hour/status`, `/activate`, `/session`, `/history` |
| `assets/watermark.png` | full-frame overlay for free-tier videos |
| `static/` | generated videos + `uploads/profile_pics` (gitignored, served publicly) |
| `alembic/versions/` | 10 migrations (otp cols, api_key, api sub fields, last_payment_id, generation source, next_credit_drop_date, play_purchase_token, Happy Hour v1 `a7c1e5d2f9b3`, Happy Hour v2 `d3f8b2a61c47`, notifications `e9b4c7a2d815` = head) |
| `docs/HAPPY_HOUR_FRONTEND_API.md` | client contract (web, Android, admin panel) for Happy Hour |
| `docs/ANDROID_INTEGRATION.md` | self-contained Android guide (auth, Play purchase, Happy Hour, FCM + inbox) to feed to the Claude instance in the Android project. **Keep it in sync** when Happy Hour / notification / payment endpoints change |
| `tests/` | `test_happy_hour.py`, `seed_happy_hour_users.py`, plus older manual payment scripts |

## Data model
`User` (table `users`): identity (email unique, hashed_password nullable for Google users, full_name, profile_pic, provider `local|google`), **workspace sub** (`plan`, `billing_cycle`, `subscription_id`, `subscription_status`, `subscription_end_date`), **API sub** (`api_plan`, `api_billing_cycle`, `api_subscription_id`, `api_subscription_status`, `api_subscription_end_date`), shared `credits` (default 20 — one ledger for both), `last_payment_id` (idempotency), `play_purchase_token`, `next_credit_drop_date`, `api_key` (`df_<urlsafe>`), `is_admin`, `is_verified`, `otp`, `otp_expiry`, `last_login_ip`.
`GenerationHistory`: user_id, video_url, created_at, `source` (`workspace` | `api` | whatever client sends).
Status strings are free-form and inconsistent: `active`, `inactive`, `cancelled`, `canceled`, `Scheduled for cancellation on YYYY-MM-DD`, or raw Dodo statuses. Code matches with `== "active"`, `"active" in x.lower()`, `"Scheduled for cancellation" in x` — be careful when touching them.

## Endpoints
- **/auth**: `register` (email+pw → OTP email, user unverified; re-register resends OTP), `verify-otp` (returns JWT), `login`, `google/login` + `google/callback` (web SSO redirect to `FRONTEND_URL/auth-success?token=`), `google/verify` (Android id_token; audience whitelist of 2 client IDs), `logout` (no-op), `me`, `regenerate-api-key`, `update-profile` (multipart; pic saved to `static/uploads/profile_pics`, URL hardcoded to `https://api.depthflow.io/...`), `change-password`, `forgot-password`, `reset-password` (OTP, 10 min). Email via ZeptoMail HTTP API (`api.zeptomail.in`).
- **/ai** (Immersity): `POST generate-3d` (image + style Orbit/Dolly/Zoom, depth/speed/duration 1–10; flow: Immersity token → upload URL → disparity → animation; flat 20 credits), `GET history` (lazy-deletes rows older than 30 min, returns max 20).
- **/ai/depthflow**: `POST generate-3d` (image + JSON `payload` form field forwarded to `DEPTHFLOW_ENGINE_URL` with `x-api-key: DEPTHFLOW_SECRET_KEY`; returns the mp4 as FileResponse with `X-*` headers: Status, Workspace, Plan, Cost, Remaining-Credits, History-ID, Video-URL), `GET logs` (api-source history; fake duration/credits values), `GET billing`.
  - Cost = `DEPTHFLOW_API_COST` (default 20) +10 if duration>20, +5 if duration>10.
- **/payments**: `create-checkout-session`, `cancel-subscription` (body `plan_type` workspace|api), `webhook` (Dodo, Standard Webhooks signature), `sync-subscription`, `history` (Dodo invoices), `verify-purchase` (Google Play), `google-play-webhook` (Pub/Sub RTDN: type 2 RENEWED -> renewal, types 12/13 -> Happy Hour invalidation; others ignored).
- **/happy-hour**: `status`, `activate`, `session`, `history` (see Happy Hour section). **/admin/settings/happy-hour** GET/PUT. **/admin/users**, **/contact** as above.

## Business rules worth remembering
- **Watermark**: free-plan users get `assets/watermark.png` overlaid via ffmpeg (`imageio_ffmpeg` binary, ultrafast, 1 thread — tuned for low RAM). `ai_router` skips watermark for ex-subscribers with status `canceled`/`cancelled`; `depthflow_ai_router` watermarks purely on `plan.lower()=="free"`. "Credit Pack" purchase flips Free → `Credit Pack` plan to remove the watermark.
- **Videos are ephemeral**: files in `static/` and history rows expire after 30 min (`cleanup_old_files` as background task; history cleanup lazy on `/history`).
- **Credits**: deducted *after* successful generation (so concurrent requests can overspend; no row locking). New users start at 20 credits (model default). An active Happy Hour session (with a free video slot) bypasses the credit check and deduction entirely; see the Happy Hour section.
- **Plans** (`PLAN_CONFIG` in payments_router = single source of truth for Dodo product IDs/credits): monthly Basic 600 / Pro 1500 / Premium 4000; yearly Basic 7200 / Pro 18000 / Premium 48000. `API_PLAN_CONFIG`: Starter 10k / Growth 30k / Pro 65k monthly; yearly product IDs are `dummy_*` placeholders. `ONE_TIME_PLANS`: Credit Pack = 900 credits. Trial pack is commented out (code paths still exist). `GOOGLE_PLAY_PLANS` maps `sub_basic_mo/sub_pro_mo/sub_basic_yr/sub_pro_yr` → credits (no Premium on Play).
- **Dodo webhook `payment.succeeded`**: user identified by `metadata.user_id` (fallback customer email). Credits come from `metadata.credits_to_add` (new/upgrade/one-time → **added on top**) or from the stored plan on renewal (**balance reset**: "use it or lose it"). Yearly subs only get 1/12 of credits and `next_credit_drop_date = now+30d`. Idempotency via `last_payment_id`. After commit, `metadata.old_sub_id` is scheduled to cancel (`cancel_at_next_billing_date`) so plan changes don't double-bill. Checkout always creates a fresh Dodo checkout (never in-place upgrade); reuses existing Dodo customer by email.
- `subscription.updated` syncs status/plan from product_id; `subscription.cancelled` resets plan to Free, clears sub id, **zeros credits**.
- **Cron** (`cron_monthly_credits.py`): for users with `next_credit_drop_date <= now` and active/scheduled status and *yearly* cycle, **sets** `credits = yearly/12` (overwrites, not adds) and reschedules +30d; else clears the date. Needs an external scheduler (Render cron etc.).
- **Google Play**: `verify-purchase` verifies via androidpublisher API (creds from `GOOGLE_PRIVATE_KEY`/`GOOGLE_PROJECT_ID`/... env vars, else `google-play-service-account.json`), is **additive** to credits, stores `play_purchase_token`; renewals come via RTDN → `process_renewal` (resets credits).

## Happy Hour tokens (v2: subscription-period entitlements + 60-min unlimited sessions)
Contract for clients: `docs/HAPPY_HOUR_FRONTEND_API.md`. Service: `happy_hour.py`. Router: `routers/happy_hour_router.py`. Tests: `python tests/test_happy_hour.py` (22 tests, temp SQLite DBs, never touches the real DB).
- **Entitlement** (`happy_hour_entitlements`, one row per user, replaced by each grant): yearly workspace sub = **4 tokens/year**, carry forward, **max 1 activation per UTC calendar month** (`last_token_activation_month`); monthly sub = **1 token/billing period**, no rollover, re-granted by the renewal payment. **Workspace plans only** (API-key plans, Free/Trial/Credit Pack never qualify). CHECK constraints keep `available + claimed <= allocated`.
- **Grants ride on billing events** (`routers/payments_router.py`, helper `_grant_happy_hour`, runs AFTER the billing commit in its own transaction so it can never block credits/plan changes; idempotent on `grant_ref` = Dodo payment id / Play orderId): Dodo `payment.succeeded` (workspace, recurring, new + renewal), Play `verify-purchase` and `process_renewal`. Expiry comes from Dodo `next_billing_date` (`_dodo_period_end`) / Play `expiryTimeMillis`, fallback +32d/+366d; also stored in `User.subscription_end_date`. A re-grant preserves `last_token_activation_month` (re-buying can't bypass the monthly rule).
- **Invalidation** (`invalidate_entitlement`: tokens=0, status CANCELLED/EXPIRED, ACTIVE session -> INVALIDATED): Dodo `subscription.cancelled`, `subscription.updated` (cancelled/expired/failed), `sync-subscription` cancelled branch / 404, Play RTDN type 12 (revoked) and 13 (expired). Play type 3 (canceled) and Dodo "Scheduled for cancellation" keep tokens until the period end. `on_hold` just makes the entitlement unusable until active again. Past `subscription_expiry_date` the entitlement flips to EXPIRED lazily.
- **Lazy bootstrap**: a paying user with no entitlement row (subscribed before the feature, or a missed webhook) gets a one-time grant on first `/status`/`/activate`.
- **Activate** (`activate_token`, `POST /happy-hour/activate`, header `Idempotency-Key`): locks the user row (`FOR UPDATE`), replays a known key, then checks session-active (409 `SESSION_ACTIVE`) -> subscription/entitlement (403 `NOT_SUBSCRIBED`) -> tokens (403 `NO_TOKENS`) -> calendar month (403 `MONTHLY_LIMIT`), deducts one token and inserts the session (`expires_at = start + 3600s`, `max_video_generations` = admin setting snapshot) in **one commit**. DB backstops: partial unique index `uq_happy_hour_one_active` (1 ACTIVE session/user), unique `(user_id, idempotency_key)`.
- **Generation gate** = reserve -> confirm/release (`begin_generation` / `confirm_generation` / `release_generation`), wired into `/ai/generate-3d` and `/ai/depthflow/generate-3d` with try/finally. `begin_generation` order: expire stale session -> entitlement usable (active sub) -> atomic `UPDATE ... SET in_flight+1 WHERE status='ACTIVE' AND expires_at>now AND successful+in_flight<max` (concurrency-safe cap). Granted => credit check/deduction skipped; `confirm` rides the endpoint's final commit (success), `release` on any failure (failed videos never count). Cap reached => session `LIMIT_REACHED` and the request **falls back to normal credit logic** (user decision). Session status values: ACTIVE, EXPIRED, LIMIT_REACHED, INVALIDATED. Known limit: a hard crash mid-render leaves `in_flight` stuck until the session expires.
- **Admin setting**: table `app_settings` (key `happy_hour_max_videos`, default 10, 1..1000). `GET/PUT /admin/settings/happy-hour` (admin only, StrictInt > 0). Applies to newly activated sessions only.
- **Errors**: `HappyHourError(status, code, detail)` -> `{"detail", "code"}` via the handler in `main.py`.
- **Endpoints**: `GET /happy-hour/status`, `POST /happy-hour/activate`, `GET /happy-hour/session`, `GET /happy-hour/history` (the v1 `/claim` was removed).
- Migrations: `a7c1e5d2f9b3` (v1, superseded) -> `d3f8b2a61c47` (v2: drops `users.happy_hour_*`, new entitlements/sessions/app_settings; idempotent, downgrade restores the v1 shape) -> `e9b4c7a2d815` (notifications).

## Happy Hour notifier (website bell + Android push + admin broadcast)
Client contract: section 10 of `docs/HAPPY_HOUR_FRONTEND_API.md`. Code: `notifications.py`, `push_fcm.py`, `routers/notifications_router.py`. Tests: `python tests/test_notifications.py`.
- **Inbox is the source of truth** (`notifications` table; unique `(user_id, dedupe_key)`). `notify()` inserts the row, commits, and only then pushes (so a push is sent only for a *new* row). It never raises and must be called **after** the business commit. Push runs off the request path in a 4-thread pool (`NOTIFY_PUSH_SYNC=true` makes it inline, used by tests).
- **FCM** (`push_fcm.py`): HTTP v1 via `google-auth` (no firebase-admin). Env: `FIREBASE_PROJECT_ID` + `FIREBASE_SERVICE_ACCOUNT_FILE` or `_JSON`; unset => push silently disabled (inbox unaffected); `FCM_DRY_RUN=true` logs payloads (current local `.env`). Dead tokens (404/`UNREGISTERED`) get `device_tokens.disabled_at`. Android channel id `happy_hour`; every push has `data.route="happy_hour"`.
- **Triggers**: token granted -> end of `_grant_happy_hour` in `payments_router.py` (`token-grant:{payment/order id}`); session live -> `POST /happy-hour/activate` when not a replay (`session-start:{id}`); ending soon (<= 10 min), ended (EXPIRED / LIMIT_REACHED / INVALIDATED, within 24 h) and the yearly "new month, activation open" notice -> `run_notification_sweep` (`session-soon:{id}`, `session-end:{id}`, `month-unlock:{uid}:{YYYY-MM}`); admin broadcast -> `POST /admin/notifications/happy-hour-broadcast` (`broadcast:{id}`, audiences `eligible` / `all_subscribers`, `dry_run`).
- **Sweep**: an asyncio loop in `main.py`'s lifespan runs it every `NOTIFY_SWEEP_SECONDS` (60) unless `NOTIFY_SWEEP_ENABLED=false`; `cron_happy_hour_notify.py` is the same thing as a one-shot. Idempotent via dedupe keys, so multiple workers / both at once are safe. It also marks past-due sessions EXPIRED. If the host sleeps idle processes, schedule the cron script every minute.
- **Web "live"**: no websocket/SSE; the site polls `GET /notifications/unread-count` (30-60 s + tab focus). Out of scope: Web Push, email, per-user opt-out, SSE.
- Known limits: broadcast fan-out is per-device sequential in the pool (fine for thousands, not millions: use FCM topics if the audience grows); `user@example.com` and other pre-existing local users receive broadcasts too (local DB only).
- Web purchase timing: Dodo webhooks can lag the checkout redirect, so the UI should call `/payments/sync-subscription` then `/happy-hour/status` (poll briefly) after returning from checkout; renewals/cancellations need no client action.
- Local helper: `python tests/seed_happy_hour_users.py` (5 users incl. `hh_admin@example.com`, password `Test@12345`; refuses non-SQLite DBs).

## Environment variables (.env, loaded with python-dotenv; never commit)
`DATABASE_URL`, `APP_SECRET_KEY`, `ZEPTOMAIL_SEND_TOKEN/FROM_ADDRESS/FROM_NAME`, `IMMERSITY_CLIENT_ID/SECRET`, `GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI`, `DODO_PAYMENTS_API_KEY`, `DODO_WEBHOOK_SECRET`, `FRONTEND_URL`, `BASE_URL` (public URL used to build video links), `DEPTHFLOW_ENGINE_URL`, `DEPTHFLOW_SECRET_KEY`, optional `DEPTHFLOW_API_COST`, notifier: `FIREBASE_PROJECT_ID`, `FIREBASE_SERVICE_ACCOUNT_FILE` / `FIREBASE_SERVICE_ACCOUNT_JSON`, `FCM_DRY_RUN`, `NOTIFY_SWEEP_ENABLED`, `NOTIFY_SWEEP_SECONDS` (all optional), optional `DODO_ENVIRONMENT` (`live_mode` default, `test_mode` for local dev; `.env` currently `live_mode`), Google Play service-account vars (`GOOGLE_PRIVATE_KEY`, `GOOGLE_PRIVATE_KEY_ID`, `GOOGLE_PROJECT_ID`, `GOOGLE_CLIENT_EMAIL`, `GOOGLE_CLIENT_CERT_URL`). Note `GOOGLE_CLIENT_ID` is shared by web SSO and the Play service-account block (name collision). `.env` is gitignored.

## Known issues / gotchas (found during review — not yet fixed)
**Security**
1. `auth_router.send_otp_email` has a **hardcoded ZeptoMail token** in source and ignores `ZEPTOMAIL_SEND_TOKEN` (an earlier fix was reverted). Move it to env and **rotate it** (it is in git history). `tests/test_payments.py` also hardcodes a webhook secret + JWT. (`.env` already holds `ZEPTOMAIL_SEND_TOKEN` with the `Zoho-enczapikey ` prefix, so switching the code to it is a one-line change.)
2. `APP_SECRET_KEY` and `DEPTHFLOW_SECRET_KEY` have insecure defaults if env is unset.
3. OTPs use `random.randint` (not `secrets`), no attempt limit/rate limit on `verify-otp`, `login`, `reset-password`; `forgot-password` leaks account existence (404).
4. API keys stored in plaintext; `/auth/me` returns the key.
5. `update-profile` trusts the uploaded file extension/name without checking `ALLOWED_IMAGE_TYPES` (defined but unused); allows changing email with no re-verification.
6. `change-password` crashes for Google-only users (`hashed_password` is None).
7. Admin `update_user` only auto-activates for plans basic/pro; no pagination cap issues (`limit` default 1,000,000).
8. Google Play RTDN webhook has no auth (Pub/Sub token not verified); `process_renewal` passes a request-scoped DB session into a background task and reads `google-play-service-account.json` only.
9. Dodo webhook returns HTTP 200 with `{"status":"error"}` on exceptions, so Dodo won't retry failed credit grants.
10. CORS `allow_origins=["*"]` with `allow_credentials=True`. `static/` is public and unauthenticated (unguessable UUID filenames only).
11. `X-Forwarded-For` first value trusted for `last_login_ip`.
**Correctness**
- Race conditions on credits (no `SELECT ... FOR UPDATE`); credits deducted after the paid upstream call.
- `ai_router.generate_3d` blanket `except Exception` turns upstream `HTTPException`s into 500s; sync `requests` calls inside `async def` block the event loop; no timeouts on several Immersity calls; Immersity image upload reuses the presigned URL as `inputImageUrl`.
- (Fixed, in `ai_router.py`) the ex-subscriber watermark check accepts both `canceled`/`cancelled`.
- (Fixed, in `payments_router.py`) `sync_subscription` no longer overrides `final_status` for cancelled/expired/past_due.
- Dodo client environment comes from `DODO_ENVIRONMENT` (default `live_mode`). Local `.env` is `live_mode`: do not start real checkouts locally.
- Happy Hour: Play subscriptions that expire never flip `subscription_status`/`plan` (only the Happy Hour entitlement is invalidated); pre-existing gap.
- `GET /ai/depthflow/logs` returns fabricated duration and a constant credit cost.
- Mixed `datetime.utcnow()` (naive) and `datetime.now(timezone.utc)`; SQLite returns naive datetimes (code patches with `.replace(tzinfo=...)` in OTP checks).
- `Base.metadata.create_all` runs at startup **and** Alembic is used — migrations may be skipped on fresh DBs; `alembic/versions` includes a "force_add" column migration, suggesting prior drift.
- `contact_router` doesn't send email; `schemas.UserCreate.plan` is accepted but ignored.
- `requirements.txt` UTF-16 + contains unused Flask packages.

## Conventions
- Single-file-per-router, direct SQLAlchemy queries in handlers (no service/repo layer); sync `def` and `async def` mixed.
- Errors via `HTTPException`; logging is a mix of `print` and `logging`.
- Schema changes: edit `models.py` → `alembic revision --autogenerate -m "..."` → `alembic upgrade head`.
- Git: `main` branch only, terse commit messages, ~128 commits. Recent work: subscription/trial/credit-pack logic and the monthly cron. Happy Hour v1/v2 and the notifier are uncommitted at the time of writing.
- Dates: naive UTC (`datetime.utcnow()`); Happy Hour serialises them as ISO-8601 with a trailing `Z`.
