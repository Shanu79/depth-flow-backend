# Admin-managed Pricing, Home Popups and Offer Banner: API contract

For the React web app and the Admin Panel. Auth is the same as everywhere else: `Authorization: Bearer <jwt>`.
**Admin** endpoints need `is_admin` (403 otherwise). **Public** endpoints need no login unless stated.
All datetimes are **UTC ISO-8601 with a trailing `Z`**; send any ISO datetime (offsets are converted to UTC).
All admin-entered text is **plain text** (HTML tags are stripped on save): render it as text, never as HTML.
Errors: `{"detail": "..."}`; gateway/pricing errors also carry `"code"`.

## 0. First-time setup (once, after deploy)
`POST /admin/pricing-plans/import-from-dodo` (admin). Reads the current Dodo price of every existing plan
(read-only) and stores it as version 1. Safe to run again. Until it has run, checkout keeps using the old built-in plans,
so nothing breaks. The response lists `imported`, `skipped` and `errors` (the 3 API-yearly plans have placeholder
product ids and are reported, not imported). After importing, fill in each plan's description/features in the admin page
(they start empty).

## 1. Pricing plans

### Public
`GET /pricing-plans?kind=workspace|api|one_time` (optional filter) returns only **active + visible plans that have a price**, ordered by `display_order`:
```json
{"server_time": "...Z", "plans": [{
  "plan_key": "Pro",              // immutable id: send THIS as plan_name to checkout
  "kind": "workspace", "billing_interval": "monthly",   // monthly | yearly | one_time
  "name": "Pro", "description": "...", "currency": "USD",
  "price_minor": 2000, "price": 20.0,   // price_minor = cents (authoritative); yearly = full year price
  "credits": 1500,                      // per purchase (yearly = full-year total; users receive 1/12 per month)
  "features": ["..."], "is_most_popular": true, "badge_text": "Most Popular",
  "display_order": 1, "pricing_version": 2 }]}
```
Cached for 10 s. Gateway product ids are never exposed. **Checkout is unchanged**:
`POST /payments/create-checkout-session {plan_name: <plan_key>, billing_cycle: <billing_interval>, quantity}`.
The backend picks the product, price and credits itself; any price the client sends is ignored. A deactivated plan returns 400.

### Admin
| Method + path | Purpose |
|---|---|
| `GET /admin/pricing-plans` | All plans: `{imported, plans:[...]}` (see below) |
| `POST /admin/pricing-plans/import-from-dodo` | One-time import (section 0) |
| `POST /admin/pricing-plans` | Create a plan (+ price) |
| `GET /admin/pricing-plans/{id}` | One plan |
| `PUT /admin/pricing-plans/{id}` | Edit display fields and/or price (send only what changed) |
| `PATCH /admin/pricing-plans/{id}/status` | `{is_active?, is_visible?}` |
| `POST /admin/pricing-plans/reorder` | `{items:[{id, display_order}]}` |
| `POST /admin/pricing-plans/{id}/sync-payment-gateway` | Retry a failed/pending price sync |

Admin plan object = the public fields plus `id`, `is_visible`, `is_active`, `sync_status`
(`SYNCED` | `SYNCING` | `FAILED` | `NO_PRICE`), `active_price` `{version, price_minor, currency, credits, gateway_product_id, activated_at}`
and `proposed_price` `{version, price_minor, currency, attempts, last_error}` (null unless a change is pending/failed).
Show **current active price vs proposed price** and `sync_status` from these.

`PUT` body (all optional): `name` (<=60), `description` (<=400), `credits`, `features` (list, <=30 items of <=140 chars),
`is_most_popular`, `badge_text` (<=30), `display_order`, `is_visible`, `is_active`, and the price fields
`price_minor` (positive integer, cents), `currency` (3 letters, default current), `tax_category`
(`digital_products|saas|e_book|edtech`, only used for brand-new plans).
`POST /admin/pricing-plans` additionally needs `plan_key`, `kind`, `billing_interval` (one_time plans use both `one_time`).

**How a price change works**
1. Display edits (name, text, features, credits, badge, order, visibility, active) save instantly and never touch the gateway, users or subscriptions.
2. When `price_minor` changes, the backend creates a **new Dodo product** at the new price (settings copied from the current product), verifies Dodo returns exactly that price, and only then makes it the active price for **new purchases**. The old product stays mapped, so **existing subscribers keep their current price** and their webhooks/renewals keep working.
3. If Dodo fails the response is **HTTP 502** `{"detail": "...", "code": "GATEWAY_SYNC_FAILED", "plan": {...}}`. The previous price stays live everywhere, any display edits in the same request are still saved, and `plan.proposed_price.last_error` explains why. Fix and press **Retry** (`POST .../sync-payment-gateway`) or submit a corrected price. Retries never create duplicate Dodo products.
4. Another admin change in progress for the same plan returns **409** `PRICING_CONFLICT`.
5. Only one plan per group (same `kind` + `billing_interval`) can be Most Popular; enabling it on another plan clears the first (badge text defaults to "Most Popular").

Credit edits apply to new purchases and to renewals of subscribers on the plan's current product; subscribers still on an older product keep that product's credits.

## 2. Offer banner (top of the pricing page)
Public: `GET /offer-banner/active` returns `{"banner": null}` or
`{"banner": {id, title, description, image_url, button_text, button_link, coupon_code, end_at}}`: only when enabled and inside its schedule (the most recently started wins if several qualify).
Admin: `GET /admin/offer-banner` (list with `state`: `LIVE|SCHEDULED|EXPIRED|DISABLED`), `POST /admin/offer-banner`, `PUT /admin/offer-banner/{id}`,
`PATCH /admin/offer-banner/{id}/status {is_active}`, `DELETE /admin/offer-banner/{id}`.
Body: `title` (required, <=120), `description` (<=1000), `image_url`, `button_text` (<=40), `button_link`, `coupon_code`, `start_at`, `end_at`, `is_active`.
Independent of popups.

## 3. Home popups
### Frontend (login required)
- `GET /popups/eligible?limit=1` returns `{"popups": [{id, title, description, image_url, button_text, button_link, coupon_code, priority}]}`:
  only popups that are ACTIVE, inside their schedule, match the user's audience (decided on the server), and were **not already shown to this user today (UTC)**, highest priority first. Show only the first.
- When the popup is actually displayed call `POST /popups/{id}/track` (returns `recorded` or `already_tracked`; safe to repeat; 404 if the user can't see it).
  After tracking, `eligible` no longer returns it until the next UTC day, so refreshes and page changes never repeat it.
- Audience: `ALL`; `FREE` = no active workspace/API subscription (Credit Pack buyers and cancelled users count as free); `PAID` = an active subscription (including "scheduled for cancellation").
- Coupons are **display-only**: show the code with a copy button; nothing is auto-applied at checkout.
- Anonymous visitors get no popups (tracking is per user).

### Admin
`GET /admin/popups[?status=&include_archived=true]`, `POST /admin/popups`, `GET|PUT /admin/popups/{id}`,
`PATCH /admin/popups/{id}/status {status: ACTIVE|INACTIVE|ARCHIVED}`, `DELETE /admin/popups/{id}` (= archive; history kept; archived popups can't be edited, restore with PATCH status).
Body: `title` (required, <=120), `description` (<=1000), `image_url`, `button_text` (<=40, required with a link), `button_link`, `coupon_code`
(3-32 of `A-Z 0-9 - _`, uppercased), `target_audience` (`ALL|FREE|PAID`), `status` (`ACTIVE|INACTIVE`, default INACTIVE), `priority` (0-1000, higher first), `start_at` (null = now), `end_at` (null = never; must be after start).
List items add `state` (`LIVE|SCHEDULED|EXPIRED|INACTIVE|ARCHIVED`) and `total_displays`.

### Image upload (popups + banner)
`POST /admin/uploads/image` (admin, multipart field `file`) returns `{"url": "<BASE_URL>/static/uploads/content/<random>.png"}`; put that into `image_url`.
JPEG / PNG / WebP only (checked by file contents, not name), max 2 MB (413 above), SVG/HTML rejected (400).

## 4. Validation summary
- Links (`button_link`): `https://...` or an in-app path starting with a single `/`. `javascript:`, `data:`, `//host`, plain `http:` are rejected (422).
- `image_url`: http(s) URL or in-app path.
- HTML tags are removed from all text fields; control characters removed.
- Only admins can reach any `/admin/*` endpoint.


## What's New window (`whats_new_releases`)

Each release is one **tab** of the "What's New" modal. Tabs are ordered by `display_order` (ties: newest first); the first
tab is rendered in the "latest" style.

### Public (no login)
`GET /whats-new` ->
```json
{
  "releases": [{
    "id": 3, "tab_label": "Latest: App Launch", "badge_text": "New App Launch", "title": "...", "subtitle": "...",
    "features": [{"icon": "rocket", "title": "Faster", "description": "..."}],
    "button1_text": "Google Play", "button1_link": "https://...", "button1_style": "google_play",
    "button2_text": "Continue to Web", "button2_link": null
  }],
  "announce_version": 3
}
```
- Only `ACTIVE` releases are returned. When the list is empty the frontend shows its built-in content.
- `announce_version` is the id of the newest active release with `auto_show = true` (or `null`). The browser opens the
  modal once when this differs from what it stored (`localStorage.whatsNewSeenVersion`). Editing a release does **not**
  change it; publishing a new auto-show release does.
- `icon` is one of `content_validation.WHATS_NEW_ICONS` (smartphone, devices, zap, gift, coins, code, toggle, film,
  sliders, rocket, sparkles, star, heart, shield, globe, bell, video, image, wand, palette, clock, check, users, trophy,
  flame, layers, cpu, cloud, mail, lock).
- `button*_link`: `https://...` opens in a new tab, `/path` navigates in the app and closes the modal, `null` just closes it.
  `button1_style`: `google_play` (needs an https link) | `light` | `dark`.

### Admin (`is_admin`)
| Method | Path | Notes |
|---|---|---|
| GET | `/admin/whats-new?include_archived=` | list + `has_any` |
| POST | `/admin/whats-new` | create (201) |
| GET/PUT | `/admin/whats-new/{id}` | archived releases cannot be edited (409) |
| PATCH | `/admin/whats-new/{id}/status` | `ACTIVE` / `INACTIVE` / `ARCHIVED` |
| DELETE | `/admin/whats-new/{id}` | archive (soft delete) |
| POST | `/admin/whats-new/reorder` | `{items: [{id, display_order}]}` |
| POST | `/admin/whats-new/import-defaults` | one-time import of the built-in "App Launch" + "Version 2.0" tabs (409 if any exist) |

Validation: text is stripped of HTML; `tab_label` <= 30, `title` <= 120, `subtitle` <= 400, <= 12 features (title <= 80,
description <= 240), link rules as for popups.
