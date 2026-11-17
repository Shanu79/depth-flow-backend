# DepthFlow Android App: Backend Integration Guide

> **Audience:** the Android project (developer + any Claude instance working in it). This file is self-contained: you do not need the backend repo. It describes the backend API the app talks to and, in detail, the **Happy Hour** feature and its **push/in-app notifications**, which the app has to implement.
> **Source of truth:** the backend repo (`depth-flow-backend`). Interactive API docs of a running server: `<BASE_URL>/docs`. If this file and the server disagree, the server wins. Report it.

App id / Play package: `com.shin.depthflow`. Backend: FastAPI (Python), JSON over HTTPS.

---

## 0. Quick facts

| Thing | Value |
|---|---|
| Base URL (local dev, emulator) | `http://10.0.2.2:8000` (emulator -> host machine). Real device on the same Wi-Fi: `http://<PC-LAN-IP>:8000` |
| Base URL (prod) | `https://api.depthflow.io` (confirm with the backend owner) |
| Auth header | `Authorization: Bearer <access_token>` |
| Token lifetime | 7 days. On any `401` -> send the user to login |
| Time format | every timestamp is **UTC ISO-8601 ending in `Z`** (e.g. `2026-11-15T12:41:23.246907Z`). Parse with `java.time.Instant.parse` (may include microseconds) |
| Error shape | `{"detail": "<message>"}`. Happy Hour errors add `"code": "<STABLE_CODE>"`. Validation errors (`422`) have `detail` as a **list** of `{loc, msg, type}` |
| Local HTTP | Android blocks cleartext by default. For the local server add a `network_security_config.xml` allowing `10.0.2.2` / your LAN IP (debug builds only) |

Dev accounts (local backend only, password `Test@12345` for all):

| Email | Plan | Happy Hour tokens | Credits |
|---|---|---|---|
| `hh_monthly@example.com` | Pro monthly | 1 | 100 |
| `hh_yearly@example.com` | Pro yearly | 4 (max 1 activation per calendar month) | 100 |
| `hh_nocredits@example.com` | Pro monthly | 1 | 0 (proves Happy Hour needs no credits) |
| `hh_free@example.com` | Free | none | 20 (not eligible) |

---

## 1. Authentication

### Google sign-in (what the app uses)
`POST /auth/google/verify`
```json
{ "id_token": "<Google ID token from Credential Manager / Google Sign-In>" }
```
-> `200 { "access_token": "<jwt>", "token_type": "bearer", "message": "Google Sign-In Successful" }`
-> `401 { "detail": "Invalid audience: ..." }` if the token was minted for a different OAuth client.
The backend only accepts ID tokens whose audience (`aud`) is one of its two whitelisted OAuth client IDs (one Web, one Android). The `aud` of the token is whichever client ID you pass as `serverClientId` when requesting it, so use the Web or Android client ID that belongs to the same Google Cloud project as the backend's; if you get `Invalid audience`, ask the backend owner to confirm the IDs. New users are created automatically (plan `Free`, 20 credits).

### Email + password (if offered)
- `POST /auth/register` `{email, password, full_name}` -> sends an OTP email. `POST /auth/verify-otp` `{email, otp}` -> returns `access_token`.
- `POST /auth/login` `{email, password}` -> `{access_token, token_type, ip_detected}`. `403` = email not verified, `401` = wrong credentials.

### Profile
`GET /auth/me` -> `{ id, email, full_name, credits, profile_pic, plan, is_admin, subscription_status, subscription_id, billing_cycle, api_key, is_verified }`.
`/auth/me` does **not** contain Happy Hour data: use `/happy-hour/status`.

---

## 2. Subscriptions via Google Play

Subscription products (Play product ids) and what they mean:

| Product id | Plan | Cycle | Credits | Happy Hour tokens |
|---|---|---|---|---|
| `sub_basic_mo` | Basic | monthly | 600 | 1 per billing period |
| `sub_pro_mo` | Pro | monthly | 1500 | 1 per billing period |
| `sub_basic_yr` | Basic | yearly | 7200 (paid out 1/12 monthly) | 4 per year |
| `sub_pro_yr` | Pro | yearly | 18000 (1/12 monthly) | 4 per year |

Any other product id is rejected with `400 "Unknown Google Play Product ID"`. There is no Premium on Play.

**Verify a purchase** (call right after Google Play Billing reports success, then acknowledge the purchase as usual):
`POST /payments/verify-purchase` (legacy alias `POST /ai/depthflow/verify-purchase` still works for old builds)
```json
{ "product_id": "sub_pro_mo", "purchase_token": "<Purchase.purchaseToken>" }
```
-> `200 { "status": "success", "plan": "Pro", "billing_cycle": "monthly", "new_balance": 1600 }`
-> `200 { "status": "ignored", "message": "Purchase already verified", "new_balance": N }` (safe to retry)
-> `400 { "detail": "Invalid purchase token or API error" }`

Rules the app should respect:
- The backend verifies with the Play Developer API itself; never trust the client for entitlement.
- **Happy Hour tokens are granted inside this call**, so call `GET /happy-hour/status` right after it returns.
- Renewals, cancellations and expiries reach the backend through Play real-time notifications (RTDN), not from the app. The app just refetches state on resume.
- A Play "canceled" subscription keeps benefits until the period ends; "expired"/"revoked" removes them (Happy Hour then shows `eligible: false`).

---

## 3. Generation (what Happy Hour changes)

### `POST /ai/depthflow/generate-3d`  (multipart/form-data)
| Part | Type | Notes |
|---|---|---|
| `file` | image | required |
| `payload` | string (JSON) | e.g. `{"render":{"duration":5}}` (default if omitted). Forwarded to the render engine |
| `request_source` | string | `"api"` default; send `"workspace"` from the app UI |

Success: `200` with the **MP4 as the response body** plus headers:
`X-Status`, `X-Plan`, `X-Cost` (credits charged; `0` during Happy Hour), `X-Remaining-Credits`, `X-History-ID`, `X-Video-URL` (public URL, **expires after ~30 min**), **`X-Happy-Hour`** (`active` | `inactive`), **`X-Happy-Hour-Remaining-Videos`** (int).
Cost without Happy Hour: 20 credits base, +5 if `render.duration` > 10, +10 if > 20.
Errors: `402` not enough credits (message includes cost and balance), `400` invalid payload, `500` render failure (nothing is charged; a Happy Hour video slot is also released).
Free-plan videos are watermarked server-side.

### `POST /ai/generate-3d`  (multipart, Immersity pipeline)
Parts: `file`, `style` (`Orbit`|`Dolly`|`Zoom`), `depth`, `speed`, `duration` (ints 1-10). Success JSON:
`{ "status":"success", "happy_hour_active": true|false, "happy_hour_remaining_videos": N, "video_url": "...", "plan": "...", "remaining_credits": N, "history_id": N }`. Flat cost 20 credits.

### Happy Hour effect on generation (server decides, never the app)
If the user has an **active subscription + a live session (before `expires_at`) + a free video slot**, the request costs 0 credits. Otherwise normal credit rules apply (so a 0-credit user gets `402` once the hour is over or the video cap is hit). **Do not gate the Generate button on local Happy Hour state alone**: send the request and read `X-Happy-Hour`; just use the local state to show "Unlimited" in the UI.

Also: `GET /ai/history` (videos from the last 30 min, max 20: `{id, video_url, created_at, expires_in_seconds, is_expired}`), `GET /ai/depthflow/billing`.

---

## 4. Happy Hour (feature spec for the app)

### Rules the UI must communicate
| Subscription | Tokens | Rules |
|---|---|---|
| **Yearly** | 4 per subscription year | any time in the year; **max 1 activation per calendar month (UTC)**; unused tokens carry forward within the year; end with the subscription |
| **Monthly** | 1 per billing period | must be used before the period ends; **no rollover**; a new token arrives after each successful renewal |
| Free / credit-pack-only / cancelled | none | not eligible |

- Activating spends one token and starts **exactly 3600 s** of unlimited generation. The server stores `expires_at`: killing the app, logging out or going offline never changes it. One session at a time; cannot be paused, extended or restarted; the token is never refunded.
- Each session has a **video cap** (set by an admin, snapshotted at activation). At the cap the session ends (`LIMIT_REACHED`) and normal credit rules apply for the remaining time.
- Failed generations don't count toward the cap.
- Cancelling/expiring the subscription voids unused tokens and ends a running session.

### `GET /happy-hour/status`  (call on app start, after login, after `verify-purchase`, after activation, on resume, when the countdown hits 0)
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
- `blocked_reason`: `null` (can activate) | `NOT_SUBSCRIBED` | `SESSION_ACTIVE` | `NO_TOKENS` | `MONTHLY_LIMIT`. Enable the Start button only when `can_activate_this_month` is true.
- `eligible=false` -> token fields are `0`; show an upgrade/subscribe CTA.
- `session` (when `active`):
```json
{ "id": 1, "status": "ACTIVE", "subscription_id": "...",
  "started_at": "...Z", "expires_at": "2026-11-15T12:04:23Z", "remaining_seconds": 3599,
  "max_video_generations": 10, "successful_video_generations": 2, "remaining_video_allowance": 8,
  "created_at": "...Z", "updated_at": "...Z" }
```
`status` values: `ACTIVE`, `EXPIRED` (time ran out), `LIMIT_REACHED` (cap hit), `INVALIDATED` (subscription ended).

### `POST /happy-hour/activate`
Header **`Idempotency-Key: <UUID>`**: generate a **new UUID for each user tap**, reuse it only when retrying that same tap after a network error/timeout (this guarantees a retry never spends a second token). No body. Max 128 chars.
`200` -> same JSON as `/status` plus `"idempotent_replay": true|false`.

| HTTP | `code` | Meaning | UI |
|---|---|---|---|
| 409 | `SESSION_ACTIVE` | a session is already running | refresh status, show countdown |
| 403 | `NO_TOKENS` | no tokens left this period | "No tokens left" (+ renewal hint) |
| 403 | `MONTHLY_LIMIT` | already activated this calendar month | "Next activation on <`next_activation_available_at`>" |
| 403 | `NOT_SUBSCRIBED` | no active Monthly/Yearly subscription | upgrade CTA |
| 401 | n/a | bad/expired JWT | re-login |

Error body: `{"detail": "<human message, safe to show>", "code": "MONTHLY_LIMIT"}`. Switch on `code`, display `detail`. Disable the button while the request is in flight.

### `GET /happy-hour/session`
`{ "server_time": "...Z", "active": true, "session": { ... } }` or `"active": false, "session": null`. Lightweight call for the countdown banner.

### `GET /happy-hour/history?limit=50`
Array of session objects (shape above), newest first (max 200).

### Countdown: display only, anchored to the server clock
```kotlin
// On every /status or /session response:
val serverNow = Instant.parse(resp.serverTime)
val skew = Duration.between(Instant.now(), serverNow)            // device clock vs server clock
val expiresAt = Instant.parse(resp.session.expiresAt)
fun secondsLeft(): Long =
    (Duration.between(Instant.now().plus(skew), expiresAt).seconds).coerceAtLeast(0)
// tick once a second (lifecycleScope + repeatOnLifecycle(STARTED)); at 0 -> refetch /status
```
- Never persist a counter. After process death/resume just call `/status` again; the timer resumes from the server's `expires_at`.
- Re-fetch on `ON_RESUME`, after every generation (or read `X-Happy-Hour-Remaining-Videos`), and when a push/inbox item arrives.

### Suggested UI states
| Condition | UI |
|---|---|
| `eligible=false` | "Happy Hour is for Monthly/Yearly subscribers" + upgrade CTA |
| `active=true` | banner "Unlimited: mm:ss left, N videos left"; Start disabled; Generate shows "Unlimited" |
| `can_activate_this_month` | "Start Happy Hour (`available_tokens` left)" |
| `MONTHLY_LIMIT` | "Already used this month. Next: <date>" |
| `NO_TOKENS` | "No tokens left" |
| last session `LIMIT_REACHED` | "Video limit reached, normal credits apply" |

---

## 5. Notifications: push (FCM) + in-app inbox

Every notice is stored in the user's **inbox** first; the same notice is also **pushed via FCM** to registered devices. The backend never sends the same event twice to a user.

| `type` | When | Example title / body |
|---|---|---|
| `HAPPY_HOUR_TOKEN_AVAILABLE` | token granted (purchase/renewal) **or** 1st of a new month for yearly users who still have tokens | "Your Happy Hour tokens are ready" / "Happy Hour is available this month" |
| `HAPPY_HOUR_STARTED` | user activated a token | "Happy Hour is live!" / "Unlimited generation for the next 60 minutes (up to 10 videos)." |
| `HAPPY_HOUR_ENDING_SOON` | ~10 min before the session ends | "Happy Hour ends soon" / "About 9 minutes left and 7 videos remaining." |
| `HAPPY_HOUR_ENDED` | time ran out, cap reached, or subscription cancelled | "Happy Hour has ended" / "Happy Hour video limit reached" |
| `HAPPY_HOUR_BROADCAST` | admin announcement | admin-written |

"Ending soon"/"ended" come from a server sweep that runs every 60 s, so they can arrive up to ~1 minute late. Treat `/happy-hour/status` as the authority.

### One-time setup in the app
1. Add Firebase to the app (`com.shin.depthflow`) and Firebase Messaging. Give the **Firebase project id** and a **service-account JSON** to the backend owner (backend env: `FIREBASE_PROJECT_ID`, `FIREBASE_SERVICE_ACCOUNT_FILE`/`_JSON`). Until that is configured the backend sends no pushes; the inbox still works.
2. Create the notification channel **`happy_hour`** (the backend sets `channel_id = "happy_hour"` on every push; without a channel with this id the notification falls back to the default channel):
```kotlin
val channel = NotificationChannel("happy_hour", "Happy Hour", NotificationManager.IMPORTANCE_HIGH)
getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
```
3. Android 13+ (API 33): request the `POST_NOTIFICATIONS` runtime permission (ask in context, e.g. when the user first sees Happy Hour). Without it pushes are silent; the in-app inbox still works.

### Register / unregister the device token
- After login **and** in `FirebaseMessagingService.onNewToken` (when logged in): 
  `POST /notifications/devices`
  ```json
  { "token": "<FCM registration token>", "platform": "android", "app_version": "1.4.0" }
  ```
  -> `200 {"status":"registered"}`. Safe to call repeatedly. If the same phone is used by another account, the token moves to whoever registered last.
- **On logout** (before discarding the JWT): `POST /notifications/devices/unregister` `{ "token": "<FCM token>" }` -> `{"status":"unregistered"}` (`"not_found"` if it wasn't registered to this user). Optionally also delete the local FCM token (`FirebaseMessaging.getInstance().deleteToken()`).
- The token must be 20-4096 chars (`422` otherwise). The backend disables tokens that FCM reports as invalid; re-register on next app start.

### Push message format (FCM HTTP v1, `notification` + `data`)
`notification.title`/`body` are shown by the system when the app is in the background. `data` (all values are **strings**) always contains:
`type` (see table), `notification_id` (inbox id), `route` = `"happy_hour"`, plus event fields: `session_id`, `expires_at`, `max_video_generations`, `remaining_videos`, `status`, `videos_used`, `available_tokens`, `subscription_type`, `month`, `broadcast_id` (whichever apply).
- Tap handling: open the Happy Hour screen when `data["route"] == "happy_hour"` (put `data` into the launcher PendingIntent extras; for background messages Android delivers `data` as intent extras to the launcher activity).
- Foreground: `onMessageReceived` is called; show your own in-app banner/notification and refetch `/happy-hour/status`.
- Mark the inbox item read: `POST /notifications/{notification_id}/read`.

```kotlin
class DepthFlowMessagingService : FirebaseMessagingService() {
    override fun onNewToken(token: String) { /* if logged in: POST /notifications/devices */ }
    override fun onMessageReceived(msg: RemoteMessage) {
        val type = msg.data["type"]; val notificationId = msg.data["notification_id"]
        // refresh Happy Hour state, show heads-up notification on channel "happy_hour" when in foreground
    }
}
```

### In-app inbox (also the fallback if a push is missed or permission is off)
`GET /notifications?limit=20&after_id=&before_id=&unread_only=false`
```json
{ "server_time": "...Z", "unread_count": 2,
  "items": [ { "id": 5, "type": "HAPPY_HOUR_STARTED", "title": "Happy Hour is live!",
               "body": "Unlimited generation for the next 60 minutes (up to 10 videos).",
               "data": { "route": "happy_hour", "session_id": 1, "expires_at": "...Z", "max_video_generations": 10 },
               "created_at": "...Z", "read_at": null } ] }
```
- Newest first. `after_id=<newest id you have>` -> only newer items. `before_id=<oldest id you have>` -> older page. `limit` 1-100.
- `GET /notifications/unread-count` -> `{"server_time": "...", "unread_count": 2}` (cheap; use for the badge, e.g. on resume).
- `POST /notifications/{id}/read` -> `{"status":"success","unread_count":N}`; `404` if missing or not yours.
- `POST /notifications/read-all` -> `{"status":"success","marked":N,"unread_count":0}`.
- Note: `data` in the inbox JSON has real JSON types (ints), while `data` in a **push** is all strings.

---

## 6. Suggested Retrofit surface (adapt to your stack)

```kotlin
interface DepthFlowApi {
    @POST("auth/google/verify") suspend fun googleVerify(@Body b: GoogleTokenReq): TokenResp
    @POST("payments/verify-purchase") suspend fun verifyPurchase(@Body b: PurchaseReq): PurchaseResp

    @GET("happy-hour/status") suspend fun hhStatus(): HappyHourStatus
    @POST("happy-hour/activate") suspend fun hhActivate(@Header("Idempotency-Key") key: String): HappyHourStatus
    @GET("happy-hour/session") suspend fun hhSession(): HappyHourSessionResp
    @GET("happy-hour/history") suspend fun hhHistory(@Query("limit") limit: Int = 50): List<HappyHourSession>

    @GET("notifications") suspend fun inbox(@Query("limit") limit: Int = 20, @Query("after_id") afterId: Long? = null,
        @Query("before_id") beforeId: Long? = null, @Query("unread_only") unreadOnly: Boolean = false): InboxResp
    @GET("notifications/unread-count") suspend fun unreadCount(): UnreadResp
    @POST("notifications/{id}/read") suspend fun markRead(@Path("id") id: Long): ReadResp
    @POST("notifications/read-all") suspend fun markAllRead(): ReadAllResp
    @POST("notifications/devices") suspend fun registerDevice(@Body b: DeviceReq)
    @POST("notifications/devices/unregister") suspend fun unregisterDevice(@Body b: DeviceUnregisterReq)

    // returns the MP4 body; read X-Happy-Hour / X-Cost / X-Remaining-Credits from response.headers()
    @Multipart @POST("ai/depthflow/generate-3d")
    suspend fun generate(@Part file: MultipartBody.Part, @Part("payload") payload: RequestBody,
                         @Part("request_source") source: RequestBody): Response<ResponseBody>
}
```
Parsing the error body for Happy Hour calls: read `{"detail","code"}` from `HttpException.response()?.errorBody()`; show `detail`, branch on `code`. Use a generous read timeout (>= 300 s) for `generate` (server allows up to 5 min), and a normal timeout for the rest. Map snake_case JSON to camelCase (e.g. Moshi `@Json(name=...)` or kotlinx `@SerialName`), and ignore unknown fields so backend additions don't crash the app.

---

## 7. Flows to implement (checklist)

1. **Login** -> store JWT -> `registerDevice(fcmToken)` -> `GET /happy-hour/status` + `GET /notifications/unread-count`.
2. **Home / generate screen:** show Happy Hour banner from `/status`; poll nothing: refetch on resume, after generation, after push.
3. **Purchase:** Billing success -> `verifyPurchase` -> acknowledge -> `GET /happy-hour/status` (tokens now present) + `/auth/me` for credits.
4. **Activate:** tap -> new UUID -> `POST /happy-hour/activate` -> render `session.expires_at` countdown; handle 403/409 codes (section 4).
5. **Generate:** call `generate`; read `X-Happy-Hour`, `X-Happy-Hour-Remaining-Videos`, `X-Remaining-Credits`; on `402` offer credits/subscription. Videos are only available ~30 min: save/share the MP4 immediately.
6. **Push tap** -> Happy Hour screen -> `/status`; mark notification read.
7. **Logout:** `unregisterDevice` -> clear JWT.
8. **401 anywhere** -> login.

## 8. Testing against the local backend
1. Backend owner runs the server (`python -m uvicorn main:app --port 8000`, `FCM_DRY_RUN=true` by default locally, which logs pushes instead of sending them) and `python tests/seed_happy_hour_users.py` to create the dev accounts above.
2. The seeded users already have a **fake** device token; register your real FCM token with `POST /notifications/devices` to receive a real push once the backend has Firebase credentials configured.
3. Happy Hour sessions last a real hour. To see "ending soon"/"ended" quickly ask the backend owner to shorten the session or move `expires_at` in the local DB.
4. Admin broadcast test: log in as `hh_admin@example.com` and call `POST /admin/notifications/happy-hour-broadcast` `{"title":"Happy Hour is live!","body":"Use your token now.","audience":"eligible","dry_run":false}`. The eligible users' inboxes (and registered devices) receive it. Use `"dry_run": true` first to see the reach.

## 9. Things the app must NOT do
- Don't decide entitlement, remaining tokens, expiry or unlimited generation on the client; the server enforces everything and the UI only reflects it.
- Don't run a local timer that survives restarts or trust the device clock for expiry (use `server_time` skew).
- Don't reuse an `Idempotency-Key` for a *new* tap, and don't generate a new one when *retrying* the same tap.
- Don't poll `/happy-hour/status` every second; one fetch + a local display timer is enough.
- Don't leave a user's FCM token registered after logout.
