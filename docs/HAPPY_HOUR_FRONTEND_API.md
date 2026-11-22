# Happy Hour: API Contract (Web, Android, Admin panel, Notifications)

Everything the web UI, the Android app and the admin panel need for the **Happy Hour Token** feature, including the **notifier** (website bell + Android push, section 10).
The backend is the single source of truth: clients only *display* state and *request* actions.

- **Base URL (local):** `http://localhost:8000` (interactive docs: `/docs`)
- **Auth:** `Authorization: Bearer <access_token>` (from `/auth/login` or the Google sign-in endpoints) or `X-API-Key`.
- **Time:** every timestamp is **UTC ISO-8601 with a trailing `Z`**. CORS is open (`*`).
- **Errors:** `{"detail": "<human message>", "code": "<STABLE_CODE>"}` for Happy Hour errors. Switch on `code`, show `detail`. Other (existing) endpoints keep plain `{"detail": ...}`.

## Endpoint index
| Method & path | Who | Purpose |
|---|---|---|
| `GET /happy-hour/status` | any logged-in user | Tokens, eligibility, live session, remaining videos |
| `POST /happy-hour/activate` | any logged-in user | Spend a token, start the 60-minute session (needs `Idempotency-Key`) |
| `GET /happy-hour/session` | any logged-in user | Live session only (countdown banner) |
| `GET /happy-hour/history` | any logged-in user | Past sessions, newest first |
| `POST /ai/generate-3d`, `POST /ai/depthflow/generate-3d` | any logged-in user | Generation (now Happy Hour aware, see section 5) |
| `GET /admin/settings/happy-hour` | admin | Read the per-session video cap |
| `PUT /admin/settings/happy-hour` | admin | Change the cap (applies to future sessions) |
| `GET /notifications`, `GET /notifications/unread-count` | any logged-in user | Notification inbox (website bell, Android in-app list) |
| `POST /notifications/{id}/read`, `POST /notifications/read-all` | any logged-in user | Mark read |
| `POST /notifications/devices`, `POST /notifications/devices/unregister` | Android | Register / remove the FCM push token |
| `POST /admin/notifications/happy-hour-broadcast` | admin | Announce "Happy Hour is live" to eligible / all subscribers (`dry_run` supported) |
| `GET /admin/notifications/broadcasts` | admin | Broadcast history |
| `POST /auth/login`, `GET /auth/me` | existing | Auth and profile (`/auth/me` has no Happy Hour data) |
| `POST /payments/verify-purchase` (Android), `POST /payments/sync-subscription` (web) | existing | Purchase flows that lead to a token grant |

## The rules (what the UI must communicate)
| Subscription | Tokens | Rules |
|---|---|---|
| **Yearly** | 4 per subscription year | Use any time during the year. **Max 1 activation per calendar month** (UTC). Unused tokens **carry forward** within the year. Tokens end when the subscription ends/cancels. |
| **Monthly** | 1 per billing period | Must be used before the period ends. **No rollover.** A new token arrives after each successful renewal. |
| Free / Trial / Credit Pack / API-key plans / cancelled | none | Not eligible. |

- Activating spends one token and starts **exactly 3600 s** of unlimited generation (no credits charged).
- The server stores the expiry; refresh, logout, app restart or closing the browser never changes it. It can't be paused, extended or restarted. Only one session at a time. The token is not refunded.
- Each session has a **video cap** set by the admin (snapshotted when you activate). When the cap is reached the session ends (`LIMIT_REACHED`) and normal credit rules apply again for the rest of the hour.
- Failed generations don't count against the cap, and a request that is still rendering when the cap is hit may fall back to normal credits (free slots are reserved while a render is in progress).
- Cancelling/expiring the subscription voids unused tokens and ends a running session.
- A subscription that is "Scheduled for cancellation" keeps its tokens until the paid period ends.

**When do tokens appear after a purchase?**
- **Web (Dodo):** the grant happens when Dodo's `payment.succeeded` webhook is processed, which can lag the checkout redirect by a few seconds. After returning from checkout call `POST /payments/sync-subscription`, then `GET /happy-hour/status`; if `eligible` is still `false`, retry a few times (e.g. every 2 s for ~20 s).
- **Android (Play):** the grant happens inside `POST /payments/verify-purchase`, so `/status` is ready right after it returns.
- **Renewals / cancellations** need no client action: re-fetch `/status` on app resume or tab focus.

---

## 1. `GET /happy-hour/status`  (call on load, after login, after activation, after a purchase, when the countdown hits 0)
```json
{
  "server_time": "2026-11-15T11:04:23.059617Z",
  "eligible": true,
  "subscription_type": "yearly",
  "total_allocated_tokens": 4,
  "available_tokens": 3,
  "total_claimed_tokens": 1,
  "last_token_activation_month": "2026-11",
  "can_activate_this_month": false,
  "blocked_reason": "MONTHLY_LIMIT",
  "next_activation_available_at": "2026-12-01T00:00:00Z",
  "entitlement_expires_at": "2027-11-16T11:04:12Z",
  "session_duration_seconds": 3600,
  "active": false,
  "session": null,
  "remaining_video_allowance": 0
}
```
| Field | Meaning |
|---|---|
| `eligible` | Has a usable entitlement (active paid workspace subscription). If `false` the token fields are `0`. |
| `blocked_reason` | `null` (can activate) · `NOT_SUBSCRIBED` · `SESSION_ACTIVE` · `NO_TOKENS` · `MONTHLY_LIMIT` |
| `can_activate_this_month` | Convenience: `blocked_reason == null`. Enable the button only if true. |
| `next_activation_available_at` | Set only when `MONTHLY_LIMIT` (first of next month, 00:00 UTC). |
| `entitlement_expires_at` | End of the paid period the tokens belong to. |
| `session` / `active` | The live session (below), else `null` / `false`. |
| `remaining_video_allowance` | Videos still free in the live session (`0` if none). |

`session` object (also used by `/session` and `/history`):
```json
{
  "id": 1, "status": "ACTIVE", "subscription_id": "sub_123",
  "started_at": "2026-11-15T11:04:23Z", "expires_at": "2026-11-15T12:04:23Z", "remaining_seconds": 3599,
  "max_video_generations": 10, "successful_video_generations": 2, "remaining_video_allowance": 8,
  "created_at": "...", "updated_at": "..."
}
```
`status`: `ACTIVE` · `EXPIRED` (time ran out) · `LIMIT_REACHED` (video cap hit) · `INVALIDATED` (subscription cancelled/expired).

## 2. `POST /happy-hour/activate`
Header **`Idempotency-Key: <uuid>`**: generate a new one for **each user tap** (`crypto.randomUUID()` / `UUID.randomUUID()`), reuse it only when retrying that same tap (network error/timeout). No body.

`200` → same shape as `/status` plus `"idempotent_replay": true|false` (`true` = that key was already used; nothing more was spent).

| HTTP | `code` | When | Suggested UI |
|---|---|---|---|
| 409 | `SESSION_ACTIVE` | A session is already running | Refresh status, show the countdown |
| 403 | `NO_TOKENS` | No tokens left this period | "No tokens left. A new one arrives {with your next renewal / next year}" |
| 403 | `MONTHLY_LIMIT` | Already activated this calendar month | "Next activation on {next_activation_available_at}" |
| 403 | `NOT_SUBSCRIBED` | No active subscription / entitlement | Upgrade/subscribe CTA |
| 401 | n/a | Bad/expired token | Re-login |

Disable the button while the request is in flight.

Error body example: `{"detail": "You have already activated a Happy Hour token this calendar month. Next activation available on 2026-12-01T00:00:00Z.", "code": "MONTHLY_LIMIT"}`
`Idempotency-Key` is optional but strongly recommended; max 128 characters.

## 3. `GET /happy-hour/session`
```json
{ "server_time": "...", "active": true, "session": { ...session object... } }   // or "active": false, "session": null
```
Light call for the countdown banner / generation screen.

## 4. `GET /happy-hour/history?limit=50`
Array of session objects, newest first (max 200).

---

## 5. Generation endpoints (behaviour changed, requests unchanged)
Every generation request is validated **on the server**: subscription active → live session before `expires_at` → a free video slot. If all hold, credits are neither checked nor deducted; otherwise the normal credit logic runs (so a user with 0 credits gets the usual `402` once the session is over or the cap is hit).

| Endpoint | While Happy Hour applies |
|---|---|
| `POST /ai/generate-3d` | JSON: `"happy_hour_active": true`, `"happy_hour_remaining_videos": N`, `remaining_credits` unchanged |
| `POST /ai/depthflow/generate-3d` | Headers: `X-Happy-Hour: active`, `X-Happy-Hour-Remaining-Videos: N`, `X-Cost: 0`, `X-Remaining-Credits` unchanged |

Not applied → `happy_hour_active: false` / `X-Happy-Hour: inactive` and the normal cost/credits.
Notes: the decision is made when the request arrives (a render that started before expiry completes). After each generation, update the "videos left" counter from the response, or re-fetch `/status`. Never gate the Generate button on client state alone.

---

## 6. Admin panel: video cap setting (admin users only)
`GET /admin/settings/happy-hour` →
```json
{ "max_videos_per_session": 10, "default": 10, "max_allowed": 1000, "note": "..." }
```
`PUT /admin/settings/happy-hour` body `{ "max_videos_per_session": 15 }`
→ `200 {"status":"success","max_videos_per_session":15}`
Validation: a **positive integer** ≤ 1000. Strings, decimals, booleans, `0` and negatives are rejected with `422` in FastAPI's standard shape:
`{"detail": [{"type": "greater_than", "loc": ["body", "max_videos_per_session"], "msg": "Input should be greater than 0", ...}]}`
(show `detail[0].msg` next to the field). Non-admin → `403 {"detail": "You do not have admin privileges"}`.
The change applies to sessions activated **afterwards**; a running session keeps the cap it started with. No deploy needed.

---

## 7. Android notes
- Same endpoints and JSON; no new auth. Use `Idempotency-Key` (a fresh `UUID.randomUUID()` per tap, reused on retry).
- **Tokens come from the subscription purchase you already verify**: after `POST /payments/verify-purchase` (Play subscription) succeeds, call `GET /happy-hour/status`; the token allocation exists by then. Renewals are granted by the Play RTDN webhook; call `/status` on app resume.
- Play `sub_*_mo` → monthly (1 token), `sub_*_yr` → yearly (4 tokens). One-time credit packs never grant tokens.
- Users who subscribed **before** this feature get their allocation automatically the first time `/status` or `/activate` is called.
- Expiry/cancellation: Play "canceled" keeps tokens until the paid period ends; "expired"/"revoked" voids them (the next `/status` shows `eligible:false`).
- The legacy `POST /ai/depthflow/verify-purchase` alias is unchanged.

## 8. Countdown (web + Android): display only
```js
const s = await api.get('/happy-hour/status');
if (s.active) {
  const skew = Date.parse(s.server_time) - Date.now();                  // device clock vs server clock
  const left = () => Math.max(0, Math.floor((Date.parse(s.session.expires_at) - (Date.now() + skew)) / 1000));
  // tick every second; when left() === 0 → refetch /status (it will be inactive) and re-enable normal limits
}
```
- On refresh/relaunch just call `/status` again; the timer resumes from the server's `expires_at`.
- Re-fetch on tab focus / app resume (expiry, month rollover or cancellation may have happened).
- Android: compute from `expires_at` and `server_time`, not from a counter you persist.

## 9. Suggested UI states
| Condition | UI |
|---|---|
| `eligible=false` | "Happy Hour is for Monthly/Yearly subscribers" + upgrade CTA |
| `active=true` | Banner "Unlimited generation: mm:ss left, N videos left"; claim disabled |
| `blocked_reason=null` | "Start Happy Hour ({available_tokens} left)" |
| `MONTHLY_LIMIT` | "Already used this month. Next: {date}" |
| `NO_TOKENS` | "No tokens left" (+ renewal hint) |
| session `LIMIT_REACHED` (in history/status) | "Video limit reached; normal credits apply" |

---

## 10. Notifications (website bell + Android push)

**How it works.** Every notice is written to the user's **inbox** first (what the website bell and the Android in-app list read). Android devices *additionally* get an FCM push for the same notice. The same event never creates two notices for a user (the server de-duplicates), so retries and duplicate webhooks are silent.

| When | `type` | Title / body (examples) | `data` |
|---|---|---|---|
| A token is granted (purchase / renewal) | `HAPPY_HOUR_TOKEN_AVAILABLE` | "Your Happy Hour tokens are ready" / "...Activate one for 60 minutes..." | `subscription_type`, `available_tokens` |
| New month (yearly users who still have tokens and have not used this month's) | `HAPPY_HOUR_TOKEN_AVAILABLE` | "Happy Hour is available this month" | `available_tokens`, `month` |
| User activates a token | `HAPPY_HOUR_STARTED` | "Happy Hour is live!" / "Unlimited generation for the next 60 minutes (up to 10 videos)." | `session_id`, `expires_at`, `max_video_generations` |
| About 10 min before the session ends | `HAPPY_HOUR_ENDING_SOON` | "Happy Hour ends soon" / "About 9 minutes left and 7 videos remaining." | `session_id`, `expires_at`, `remaining_videos` |
| Session over (time, video cap or subscription cancelled) | `HAPPY_HOUR_ENDED` | "Happy Hour has ended" / "Happy Hour video limit reached" | `session_id`, `status` (`EXPIRED` / `LIMIT_REACHED` / `INVALIDATED`), `videos_used` |
| Admin announcement | `HAPPY_HOUR_BROADCAST` | written by the admin | `broadcast_id` |

Every notice carries `data.route = "happy_hour"`: open the Happy Hour screen when it is tapped.
"Ending soon" / "ended" come from a server sweep that runs every 60 s, so they arrive within about a minute of the real time. The authoritative state is always `GET /happy-hour/status`.

### Inbox (website + Android)
`GET /notifications?limit=20&after_id=&before_id=&unread_only=false`
```json
{
  "server_time": "2026-11-15T11:41:21Z",
  "unread_count": 2,
  "items": [
    { "id": 5, "type": "HAPPY_HOUR_STARTED", "title": "Happy Hour is live!",
      "body": "Unlimited generation for the next 60 minutes (up to 10 videos).",
      "data": { "route": "happy_hour", "session_id": 1, "expires_at": "2026-11-15T12:41:23Z", "max_video_generations": 10 },
      "created_at": "2026-11-15T11:41:23Z", "read_at": null }
  ]
}
```
- Newest first. `after_id=<last id you have>` returns only newer items (incremental polling); `before_id=<oldest id you have>` pages backwards. `limit` is 1-100.
- `GET /notifications/unread-count` → `{"server_time": "...", "unread_count": 2}`: the cheap call for the badge.
- `POST /notifications/{id}/read` → `{"status":"success","unread_count":N}` (`404` if it does not exist or is not yours). `POST /notifications/read-all` → `{"status":"success","marked":N,"unread_count":0}`.

**Website "live" behaviour (no websocket needed):** poll `unread-count` every 30-60 s while the tab is visible and again on tab focus. When it increases (or when the bell is opened) call `GET /notifications?after_id=<lastId>`. When a `HAPPY_HOUR_STARTED` / `HAPPY_HOUR_ENDED` notice arrives, also re-fetch `/happy-hour/status` so the banner and button update.

### Android push (FCM)
1. Create the Firebase project, add the app and Firebase Messaging, and give the backend owner the **service-account JSON** (see "Backend setup" below).
2. Create the notification channel **`happy_hour`** on the device (the backend sets `channel_id: "happy_hour"` on every push). On Android 13+ request the `POST_NOTIFICATIONS` permission.
3. After login, and in `FirebaseMessagingService.onNewToken`, register the token: `POST /notifications/devices` with `{"token": "<fcm token>", "platform": "android", "app_version": "1.4.0"}` → `{"status":"registered"}`. Re-registering is safe; a token used by a different account moves to the current user.
4. On logout: `POST /notifications/devices/unregister` with `{"token": "<fcm token>"}` → `{"status":"unregistered"}` (`"not_found"` if it was not yours).
5. Push shape (FCM HTTP v1): `notification {title, body}` plus `data` (all string values): `type`, `notification_id`, `route`, and the fields from the table above. When `data.route == "happy_hour"` open the Happy Hour screen, and mark the item read using `notification_id`.
6. The same notices are available in-app via `GET /notifications`: use it for the in-app list and as the fallback when a push is missed or notification permission is off.

### Admin broadcast
`POST /admin/notifications/happy-hour-broadcast`
```json
{ "title": "Happy Hour is live!", "body": "Use your token now for 60 minutes of unlimited videos.",
  "audience": "eligible", "dry_run": true }
```
- `title` 1-65 chars, `body` 1-240 chars. `audience`: `eligible` (default: subscribers who can activate right now: tokens left, not used this month, no running session) or `all_subscribers` (every active Monthly/Yearly subscriber).
- `dry_run: true` → `{"dry_run": true, "broadcast_id": null, "recipients": 120, "devices": 87}` and nothing is sent. Use it to show "this will reach N people" before the admin confirms.
- Real send → `{"dry_run": false, "broadcast_id": 7, "recipients": 120, "devices": 87}`: every recipient gets an inbox row and Android devices get the push.
- `GET /admin/notifications/broadcasts` → history `[ {id, title, body, audience, created_by_user_id, recipient_count, device_count, created_at} ]`.
- Errors: `403` non-admin, `422` validation (empty or too-long title/body, unknown audience).

### Backend setup (done by the backend owner, not the clients)
Set in `.env`: `FIREBASE_PROJECT_ID`, and `FIREBASE_SERVICE_ACCOUNT_FILE` (path to the JSON) **or** `FIREBASE_SERVICE_ACCOUNT_JSON` (the JSON on one line). Until they are set, push is disabled and **the inbox still works**. `FCM_DRY_RUN=true` logs push payloads instead of sending them (local development). `NOTIFY_SWEEP_ENABLED` (default `true`) and `NOTIFY_SWEEP_SECONDS` (default 60) control the server sweep. If your host sleeps idle web processes, also schedule `python cron_happy_hour_notify.py` every minute.

---

## Local test setup
```powershell
cd "d:\Freelancing projects\backend\depth-flow-backend"
.\.venv\Scripts\Activate.ps1
alembic upgrade head                       # already applied to the local DB
python tests/seed_happy_hour_users.py      # creates/resets the users below (local SQLite only)
python -m uvicorn main:app --reload --port 8000
python tests/test_happy_hour.py            # 22 backend tests (own temp DBs, never touches yours)
python tests/test_notifications.py         # 10 notifier tests (FCM stubbed)
```
| Email (password `Test@12345`) | Plan | Tokens | Credits | Use |
|---|---|---|---|---|
| `hh_monthly@example.com` | Pro monthly | 1 | 100 | basic flow |
| `hh_yearly@example.com` | Pro yearly | 4 (1/month) | 100 | carry-forward, monthly limit |
| `hh_nocredits@example.com` | Pro monthly | 1 | 0 | unlimited bypasses credits (402 before, passes after) |
| `hh_free@example.com` | Free | none | 20 | not-eligible UI |
| `hh_admin@example.com` | Free (admin) | none | 20 | admin setting |

Re-run the seed script any time to reset tokens, sessions, credits and notifications. Each seeded user also gets a fake Android device token, and `.env` has `FCM_DRY_RUN=true`, so the server log shows every push payload (`[FCM dry-run] ...`) without Firebase.

**Testing notes**
- A real session lasts an hour. To test expiry quickly, temporarily set `HAPPY_HOUR_SECONDS = 60` in `happy_hour.py` and restart (then set it back to `3600`).
- To see the cap quickly, set it to `2` via the admin endpoint, activate, and generate 3 videos: the 3rd falls back to credits.
- `/ai/depthflow/generate-3d` needs the engine at `DEPTHFLOW_ENGINE_URL` (`http://localhost:8001`); `/ai/generate-3d` needs valid Immersity keys. Without them the gate still works (you get a `500` from upstream, not a `402`, credits untouched, and the video slot is released).
- To see "ending soon"/"ended" without waiting an hour: start the server with `NOTIFY_SWEEP_SECONDS=5`, activate, then move the session's `expires_at` in the local DB to 8 minutes ahead (ending soon) and then into the past (ended).
- `.env` has `DODO_ENVIRONMENT=live_mode`. Don't start real checkouts locally.
