"""
Tests for the places where the admin-content feature and Happy Hour meet (the merged code).
Run:  python tests/test_integration_merge.py

Reuses the throw-away SQLite harness of tests/test_happy_hour.py (never touches the configured database).
"""
import base64
import json
import os
import sys
import traceback
import uuid
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_happy_hour as T  # noqa: E402  (sets DATABASE_URL, chdir, and provides _http_env / make_user / new_db)

import happy_hour as hh  # noqa: E402
from database import Base  # noqa: E402
from models import User, HappyHourEntitlement, HappyHourSession, PricingPlan, PlanPrice  # noqa: E402


def _signed_sender(client, secret):
    from standardwebhooks.webhooks import Webhook

    def send(event, data):
        body = json.dumps({"type": event, "data": data})
        mid = f"msg_{uuid.uuid4()}"
        ts = datetime.now(timezone.utc)
        sig = Webhook(secret).sign(mid, ts, body)
        r = client.post("/payments/webhook", content=body, headers={
            "webhook-id": mid, "webhook-timestamp": str(int(ts.timestamp())),
            "webhook-signature": sig, "content-type": "application/json"})
        assert r.status_code == 200, r.text
        return r.json()
    return send


def _with_webhook(fn):
    from routers import payments_router as pr
    c, S, state, _Fake, cleanup = T._http_env()
    secret = "whsec_" + base64.b64encode(b"integration-secret-0123456789").decode()
    saved = (pr.WEBHOOK_SECRET, pr._dodo_period_end)
    pr.WEBHOOK_SECRET = secret
    pr._dodo_period_end = lambda sub_id: hh.utcnow() + timedelta(days=365)
    try:
        fn(c, S, _signed_sender(c, secret))
    finally:
        pr.WEBHOOK_SECRET, pr._dodo_period_end = saved
        cleanup()


def _ent(S, uid):
    d = S()
    d.expire_all()
    e = d.query(HappyHourEntitlement).filter_by(user_id=uid).first()
    out = None if e is None else (e.available_tokens, e.entitlement_status)
    d.close()
    return out


# ============================================================================================ tests
def test_01_subscription_updated_cancelled_voids_happy_hour_tokens_and_still_maps_the_plan():
    """The hunk merged by hand: Happy Hour's void-on-cancel AND the pricing-table product lookup in one webhook."""
    def body(c, S, send):
        d = S()
        u = T.make_user(d, "yearly@x.com", plan="Pro", cycle="yearly", sub_id="sub_y")
        T.grant(d, u, "yearly", "ref-1", hh.utcnow(), expiry=hh.utcnow() + timedelta(days=300), sub_id="sub_y")
        uid = u.id
        d.close()
        assert _ent(S, uid) == (4, "ACTIVE")
        # an ordinary update (still active) must NOT touch the tokens; the legacy Premium monthly product id resolves via pricing.py
        send("subscription.updated", {"subscription_id": "sub_y", "product_id": "pdt_0Nc5Ts7erauRnehAFJV6Q", "status": "active"})
        d = S(); x = d.query(User).get(uid)
        assert (x.plan, x.billing_cycle, x.subscription_status) == ("Premium", "monthly", "active"), (x.plan, x.billing_cycle)
        d.close()
        assert _ent(S, uid) == (4, "ACTIVE")
        # a cancelled / expired update voids unused tokens and ends a running session (Happy Hour), status is stored
        d = S(); hh.activate_token(d, d.query(User).get(uid), "k1", now=hh.utcnow()); d.close()
        send("subscription.updated", {"subscription_id": "sub_y", "product_id": "pdt_0Nc5Ts7erauRnehAFJV6Q", "status": "cancelled"})
        assert _ent(S, uid) == (0, "CANCELLED")
        d = S()
        assert d.query(HappyHourSession).filter_by(user_id=uid, status="INVALIDATED").count() == 1
        assert d.query(User).get(uid).subscription_status == "cancelled"
        d.close()
    _with_webhook(body)


def test_02_admin_managed_retired_product_still_resolves_for_a_subscriber_with_happy_hour_tokens():
    def body(c, S, send):
        d = S()
        plan = PricingPlan(plan_key="Starter", kind="workspace", billing_interval="monthly", name="Starter",
                           currency="USD", credits=300, is_visible=True, is_active=True, pricing_version=2)
        d.add(plan)
        d.flush()
        d.add(PlanPrice(plan_id=plan.id, version=1, price_minor=500, currency="USD", credits=300, gateway_product_id="pdt_old_v1",
                        status="RETIRED"))
        d.add(PlanPrice(plan_id=plan.id, version=2, price_minor=600, currency="USD", credits=300, gateway_product_id="pdt_new_v2",
                        status="ACTIVE"))
        u = T.make_user(d, "old@x.com", plan="Free", cycle="monthly", sub_id="sub_m")
        T.grant(d, u, "monthly", "ref-m", hh.utcnow(), expiry=hh.utcnow() + timedelta(days=30), sub_id="sub_m")
        uid = u.id
        d.commit()
        d.close()
        # the subscriber is still on the RETIRED product after the admin changed the price: the plan must still resolve
        send("subscription.updated", {"subscription_id": "sub_m", "product_id": "pdt_old_v1", "status": "active"})
        d = S(); x = d.query(User).get(uid)
        assert (x.plan, x.billing_cycle) == ("Starter", "monthly")
        d.close()
        assert _ent(S, uid) == (1, "ACTIVE")                      # and their monthly token is untouched
    _with_webhook(body)


def test_03_both_features_register_their_tables_and_routes():
    import main
    tables = set(Base.metadata.tables)
    feature = {"pricing_plans", "plan_prices", "popups", "popup_displays", "offer_banners", "promo_templates", "whats_new_releases"}
    happy = {"happy_hour_entitlements", "happy_hour_sessions", "notifications", "device_tokens", "notification_broadcasts", "app_settings"}
    assert feature <= tables and happy <= tables, (feature | happy) - tables
    paths = {getattr(r, "path", "") for r in main.app.routes}
    for p in ("/popups/eligible", "/offer-banner/active", "/pricing-plans", "/whats-new", "/admin/pricing-plans", "/admin/whats-new",
              "/admin/promo-templates", "/admin/uploads/image", "/happy-hour/status", "/happy-hour/activate",
              "/notifications", "/admin/settings/happy-hour", "/admin/notifications/happy-hour-broadcast"):
        assert p in paths, f"missing route {p}"


def test_04_alembic_has_a_single_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    heads = ScriptDirectory.from_config(Config(os.path.join(T.ROOT, "alembic.ini"))).get_heads()
    assert heads == ["f1a2b3c4d5e6"], heads


# ============================================================================================ runner
def main_():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    try:
        for name, fn in tests:
            try:
                fn()
                print(f"PASS  {name}")
            except Exception:
                failed += 1
                print(f"FAIL  {name}")
                traceback.print_exc()
    finally:
        for e in T._engines:
            e.dispose()
        T.shutil.rmtree(T.TMP, ignore_errors=True)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_())
