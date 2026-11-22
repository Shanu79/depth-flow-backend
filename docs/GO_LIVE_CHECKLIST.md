# Go-live checklist: admin-managed pricing, popups, offer banner, What's New

Applies to branch `feature/admin-pricing-popups` in **both** repos (backend and frontend). Migration: `c5d7a1f3e902`
(its parent is `b045d750bfe0`, the current head on `main`).

> **Read this first: price changes made in the admin panel create real products in whichever Dodo account the server is
> pointed at.** The environment is chosen by `DODO_ENVIRONMENT` (`live_mode` or `test_mode`; if it is missing the app uses
> `live_mode`, so production is unchanged). A **test** API key only works with `test_mode` and a **live** key only with
> `live_mode`; a mismatch shows up as `401 Unauthorized`. The live products (your real plan ids) exist only in the live account.
> Step 6 explains the rehearsal script that checks all of this safely.

---

## 0. Before you deploy

- [ ] Commit both repos' `feature/admin-pricing-popups` branches and review the diffs. (They are **not** committed or pushed yet.)
- [ ] Decide the merge order: **this feature first, Happy Hour after.** When Happy Hour is rebased onto it, expect conflicts in
      `main.py`, `models.py` and `routers/payments_router.py`, and its migration `a7c1e5d2f9b3` must be re-pointed so its
      `down_revision` is `c5d7a1f3e902`.
- [ ] Confirm the **frontend production API URL** is correct in `src/config.js`. `REACT_APP_DEV_API` is a dev-only override
      and must not be set in the production build.
- [ ] Back up the production database (for Postgres: `pg_dump`). Keep the file until step 8 is done.
- [ ] Run the checks locally one last time:
      `python tests/test_pricing_popups.py` (expect 29/29) and `npx react-scripts test --watchAll=false src/components` in the frontend (expect all pass).

## 1. Server configuration

- [ ] `BASE_URL` is the public **https** URL of the API (for example `https://api.depthflow.io`). Uploaded image links are built from
      it; a wrong value gives broken images, and `http` gives mixed-content errors on an https site.
- [ ] `DATABASE_URL` points at production (Postgres). `DODO_PAYMENTS_API_KEY` is the **live** key, `DODO_WEBHOOK_SECRET` is set, and
      `DODO_ENVIRONMENT` is `live_mode` (or simply not set). **Never copy a `test_mode` value, or the test key from a local
      `.env`, to production.**
- [ ] **Uploaded images must survive restarts and deploys.** They are saved on the server disk under `static/uploads/content/`
      and served from `/static/`. If your host has an ephemeral disk (many PaaS hosts do), mount a persistent volume for that
      folder, or uploads will disappear on the next deploy and popups/banners will show broken images.
- [ ] CORS already allows your frontend origin (the new endpoints are served by the same app, so nothing new is needed; just confirm).

## 2. Deploy the backend and run the migration

- [ ] Deploy the backend.
- [ ] Run `alembic current` (expect `b045d750bfe0`), then `alembic upgrade head`. The migration is idempotent: the app also
      runs `create_all` at startup, so a table that already exists is skipped.
- [ ] `alembic heads` shows a single head: `c5d7a1f3e902`.
- [ ] These tables exist: `pricing_plans`, `plan_prices`, `popups`, `popup_displays`, `offer_banners`, `promo_templates`, `whats_new_releases`.

## 3. Backend smoke test (before the frontend goes out)

Nothing should change for visitors yet. Until an admin imports the plans, the site keeps using the built-in plans.

- [ ] `GET /pricing-plans` returns the built-in plans (fallback) with the same prices as before.
- [ ] `GET /offer-banner/active` returns `{"banner": null}`; `GET /whats-new` returns no releases.
- [ ] Start a checkout for each plan type (workspace monthly, yearly, API, credit pack) **without paying**, and confirm the session
      is created for the same Dodo product as before the release. Checkout, webhooks and renewals share code with the new pricing
      service, so this is the most important regression check.
- [ ] Admin endpoints return **403** for a non-admin user and for no login (for example `GET /admin/pricing-plans`).

## 4. Deploy the frontend

- [ ] Build and deploy the frontend.
- [ ] `/pricing` still shows the same plans, prices and buttons. With no admin data it falls back to its built-in content.
- [ ] The "What's New" window still opens from the navbar with the built-in content, and opens once for a new visitor.
- [ ] `/admin/popups`, `/admin/pricing`, `/admin/offer-banner` and `/admin/whats-new` open for an admin and redirect everyone else.
- [ ] Sanity check: only the people who should have admin access have `is_admin` set. Edits are live immediately and there is no audit
      log of who changed what, only a `created_by_user_id` on created rows.

## 5. Import the plans (one time)

- [ ] `/admin/pricing` -> **Import from payment gateway**. This only **reads** from Dodo.
- [ ] Compare every imported price with what Dodo actually charges (workspace, API and credit pack tiles). The import takes each
      price from Dodo.
- [ ] The three API **yearly** plans have placeholder product ids in the code, so they are reported as skipped, not imported. That is expected.
- [ ] Imported plans start with no description or features; the pricing page keeps showing the built-in text for those until you fill them in.
- [ ] Open `/pricing` and compare with the old page (prices, badges, the yearly "% off" pill, the Free card, the credit pack).
- [ ] Do **not** edit any price in the admin panel yet; run step 6 first.

## 6. Rehearse the price change with the script

`scripts/price_cycle_check.py` raises each plan's price by $0.10 through the real admin endpoints, checks it in Dodo, reverts
it, and checks again. It always uses a **scratch database**, never production. Its only lasting effect is new products in
the Dodo account selected by `DODO_ENVIRONMENT` (two per plan). It refuses to create anything in `live_mode` unless you pass
`--confirm-live`, and it stops at the first failed check.

Run it from a machine that has the **live** `DODO_PAYMENTS_API_KEY` (environment variable or `.env`; never paste the key into chat or a ticket):

1. [ ] **Read-only pre-flight:** `python scripts/price_cycle_check.py --env live_mode --dry-run`.
       It must list every plan with the price you expect (the three API *yearly* plans are skipped; they have placeholder ids).
       A `401` means the key does not match the environment; a `404` means the product id is not in that account.
2. [ ] **One plan first:** `python scripts/price_cycle_check.py --env live_mode --confirm-live --plan "Basic/monthly"`.
       Expect "+0.10 ok ... settings identical" and "revert ok", then check the two new products in the Dodo dashboard.
3. [ ] **The rest:** `python scripts/price_cycle_check.py --env live_mode --confirm-live --all`.
       Products from step 2 are found again by their metadata, not duplicated.
4. [ ] **Do not archive** the products it created. If the same plan, version and price is used again the app reuses them,
       and an archived product would break that.

What the script verifies for each plan: the new product has exactly the new price; the original product's price is unchanged;
all other settings (billing interval, subscription period, tax category) are identical to the original; the public pricing
endpoint shows the new price; after the revert the price is back, and the plan's history is v1 retired, v2 retired, v3 active.

Rehearsal in test mode (no live key needed): `python scripts/price_cycle_check.py --env test_mode --rehearsal-products --all`
uses the DepthFlow Basic/Pro products in your Dodo **test** account. It last passed for all four.

After the script, make the first **real** price change in the admin panel on a low-traffic plan:

- [ ] Expect a confirmation prompt; confirm the new price.
- [ ] A new product appears in Dodo with the right price and the same settings as the old one.
- [ ] The plan shows the new active price (version 2) and a "SYNCED" status.
- [ ] A fresh checkout for that plan uses the **new** product; an existing subscriber's renewal still uses their **old** price.
- [ ] If the sync fails the page shows the error and a Retry button; the old price stays live. Retrying does not create a second product.

## 7. Set up the content

- [ ] **What's New:** `/admin/whats-new` -> **Import current content**, then edit. Only tick "Open automatically, once" on a release you
      really want everyone to see once.
- [ ] **Popups:** create each popup as **Inactive**, check the preview, then activate. Use start and end dates to schedule. Remember a popup
      is shown once per user per UTC day, and only to logged-in users.
- [ ] **Offer banner:** same approach. Only one banner shows at a time (the most recently started one).
- [ ] **Images:** upload one, open the returned link in a private browser window to confirm it loads over https, then redeploy or restart
      and confirm it still loads (this proves the storage from step 1). Recommended sizes are shown in the form
      (popup 1080 x 1350, banner 1920 x 300, phone banner 800 x 400; JPEG/PNG/WebP up to 5 MB).
- [ ] Coupon codes in popups and banners are **display only**; they are not applied automatically at checkout.

## 8. Rollback

| Problem | Fix | Effect |
|---|---|---|
| A popup, banner or What's New tab is wrong | Deactivate or archive it in the admin panel | Instant, no deploy |
| A plan is wrong or should not be sold | Hide and/or deactivate it | Instant. Inactive plans cannot be bought |
| A price is wrong | Edit the price back | Creates another Dodo product; the old ones stay mapped so nobody is orphaned |
| The release is broken | Redeploy the previous backend and frontend builds | The admin tables stay in the database, unused. The site goes back to the built-in plans |
| You want the tables gone | `alembic downgrade b045d750bfe0` | **Do not do this after any customer has bought at a price created in the admin.** Their product ids exist only in the new tables, so renewals and webhooks for them could no longer be matched |

## 9. First days after launch

- [ ] Watch the logs for 5xx errors and `GATEWAY_SYNC_FAILED`.
- [ ] Watch the first real purchases and renewals after any price change: payment webhooks should be processed and credits granted as before.
- [ ] Keep the pre-release database backup until you are comfortable.
