"""
Run the admin features (pricing, popups, offer banner) locally WITHOUT touching real Dodo or your real database.

    python tests/local_admin_demo.py            # keeps the demo data between runs
    python tests/local_admin_demo.py --reset    # start from an empty demo database
    python tests/local_admin_demo.py --port 8020   # run on another port (own database file)

What it does
  * serves the real app on http://localhost:8010 using a throw-away SQLite file in your temp folder
  * replaces the payment gateway with an in-memory FAKE (the plans import with the usual prices, a price change
    "creates" a fake product): nothing is ever sent to Dodo
  * seeds three logins (password  Demo@12345 ):
        demo_admin@example.com   admin
        demo_free@example.com    free user
        demo_paid@example.com    paid (Pro monthly) user
  * demo helpers (open in the browser):
        http://localhost:8010/__demo/fail-next    make the NEXT price sync fail (to see the error + Retry)
        http://localhost:8010/__demo/gateway      see the fake products that were "created"
        http://localhost:8010/__demo/reset-popups let the seeded users see popups again today

Frontend: from the frontend folder run   set REACT_APP_DEV_API=http://localhost:8010 && npm start
(PowerShell:  $env:REACT_APP_DEV_API="http://localhost:8010"; npm start)
"""
import os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

PORT = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8010   # --port N: run beside another demo
DB = os.path.join(tempfile.gettempdir(), "depthflow_admin_demo.db" if PORT == 8010 else f"depthflow_admin_demo_{PORT}.db")
if "--reset" in sys.argv and os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_URL"] = "sqlite:///" + DB.replace("\\", "/")       # never your real DB
os.environ["BASE_URL"] = f"http://localhost:{PORT}"

import uvicorn
from fastapi import APIRouter

import main
from auth import get_password_hash
from database import SessionLocal
from datetime import datetime, timedelta
from sqlalchemy import inspect, text
from database import engine
from models import User, PopupDisplay, Popup, OfferBanner
from pricing_gateway import GatewayError
from routers import pricing_router

# prices the "live" account would report for the existing product ids (minor units = cents)
LEGACY_PRICES = {
    "pdt_0NUQxXnGKlkhrpGBAFMvy": 1000, "pdt_0NUQxXJa0TR6vJJznsrt2": 2000, "pdt_0Nc5Ts7erauRnehAFJV6Q": 4000,
    "pdt_0NUQxWNFaRWTd8eoNkqoJ": 10800, "pdt_0NUQxVyW088PIAwIzObuC": 21600, "pdt_0Nc5TfN5t1oLCfoeNN7AZ": 43200,
    "pdt_0Nbt4jmPtgBIPmJgF3qdh": 4900, "pdt_0NbtLLrNFAwrGkvpH0lGQ": 9900, "pdt_0Nbt4bIY0qlHH3u8SMyyD": 19900,
    "pdt_0NYCtBgPqWWzyF6yKrE98": 1500,
}


class FakeGateway:
    """Same two methods as pricing_gateway.DodoGateway, entirely in memory."""

    def __init__(self):
        self.products = {pid: {"price_minor": p, "currency": "USD", "sync_key": None}
                         for pid, p in LEGACY_PRICES.items()}
        self.created = 0
        self.fail_next = False

    def read_product(self, product_id):
        p = self.products.get(product_id)
        if p is None:
            raise GatewayError(f"Could not read Dodo product {product_id}: not found (fake gateway)")
        return {"price_minor": p["price_minor"], "currency": p["currency"]}

    def ensure_product(self, sync_key, spec):
        if self.fail_next:
            self.fail_next = False
            raise GatewayError("Dodo is unreachable (simulated by the demo)")
        pid = next((k for k, v in self.products.items() if v["sync_key"] == sync_key), None)
        if pid is None:
            self.created += 1
            pid = f"pdt_demo_{self.created}"
            self.products[pid] = {"price_minor": spec["price_minor"], "currency": spec["currency"],
                                  "sync_key": sync_key}
        return pid


GW = FakeGateway()
main.app.dependency_overrides[pricing_router.get_gateway] = lambda: GW


# The demo users have made-up subscription ids ("sub_seed_*", "sub_demo"). The app syncs a user's subscription with Dodo on
# every page load and treats "404 not found" as "cancelled" (which also voids Happy Hour tokens), so answer for those ids
# locally. Real subscription ids still go to Dodo.
from types import SimpleNamespace  # noqa: E402
from routers import payments_router as _pr  # noqa: E402


class _SubsProxy:
    def __init__(self, real):
        self._real = real

    def retrieve(self, subscription_id, **kw):
        if str(subscription_id).startswith(("sub_seed_", "sub_demo")):
            return SimpleNamespace(status="active", product_id="pdt_0NUQxXJa0TR6vJJznsrt2",
                                   cancel_at_next_billing_date=False, next_billing_date=None)
        return self._real.retrieve(subscription_id=subscription_id, **kw)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _ClientProxy:
    def __init__(self, real):
        self._real = real
        self.subscriptions = _SubsProxy(real.subscriptions)

    def __getattr__(self, name):
        return getattr(self._real, name)


_pr.client = _ClientProxy(_pr.client)

demo = APIRouter(prefix="/__demo", tags=["demo helpers"])


@demo.get("/fail-next")
def fail_next():
    GW.fail_next = True
    return {"ok": True, "note": "The next price sync will fail once. Retry it from the admin page."}


@demo.get("/gateway")
def gateway():
    return {"fake_products_created": {k: v["price_minor"] for k, v in GW.products.items() if k.startswith("pdt_demo_")}}


@demo.get("/reset-popups")
def reset_popups():
    db = SessionLocal()
    try:
        n = db.query(PopupDisplay).delete()
        db.commit()
        return {"ok": True, "cleared_display_records": n}
    finally:
        db.close()


main.app.include_router(demo)

# older demo databases were created before popups had button_color
if "popups" in inspect(engine).get_table_names():
    _have = {c["name"] for c in inspect(engine).get_columns("popups")}
    with engine.begin() as conn:
        for _name, _ddl in (("button_color", "VARCHAR"), ("text_color", "VARCHAR"), ("bg_color", "VARCHAR"),
                            ("accent_color", "VARCHAR"), ("header_text", "VARCHAR"), ("footnote", "VARCHAR"),
                            ("layout", "VARCHAR NOT NULL DEFAULT 'split'"),
                            ("show_countdown", "BOOLEAN NOT NULL DEFAULT 0")):
            if _name not in _have:
                conn.execute(text(f"ALTER TABLE popups ADD COLUMN {_name} {_ddl}"))

if "offer_banners" in inspect(engine).get_table_names():
    _haveb = {c["name"] for c in inspect(engine).get_columns("offer_banners")}
    with engine.begin() as conn:
        for _name, _ddl in (("button_color", "VARCHAR"), ("text_color", "VARCHAR"), ("bg_color", "VARCHAR"),
                            ("accent_color", "VARCHAR"), ("header_text", "VARCHAR"), ("badge_text", "VARCHAR"),
                            ("show_countdown", "BOOLEAN NOT NULL DEFAULT 0")):
            if _name not in _haveb:
                conn.execute(text(f"ALTER TABLE offer_banners ADD COLUMN {_name} {_ddl}"))

for _table in ("popups", "offer_banners"):
    if _table in inspect(engine).get_table_names():
        _haves = {c["name"] for c in inspect(engine).get_columns(_table)}
        with engine.begin() as conn:
            for _name, _ddl in (("bg_color2", "VARCHAR"), ("theme", "VARCHAR NOT NULL DEFAULT 'none'"),
                                ("emoji", "VARCHAR"), ("font_style", "VARCHAR NOT NULL DEFAULT 'modern'"),
                                ("bg_image_url", "VARCHAR"),
                                ("display_mode", "VARCHAR NOT NULL DEFAULT 'designed'")):
                if _name not in _haves:
                    conn.execute(text(f"ALTER TABLE {_table} ADD COLUMN {_name} {_ddl}"))
            if _table == "offer_banners" and "mobile_image_url" not in _haves:
                conn.execute(text("ALTER TABLE offer_banners ADD COLUMN mobile_image_url VARCHAR"))

db = SessionLocal()
try:
    # sample content so the admin pages (and the live popup / pricing banner) look good on first visit
    _img = "https://images.unsplash.com/photo-1470071459604-3b5ec3a7fe05?auto=format&fit=crop&q=80"
    if db.query(Popup).filter(Popup.title == "Image-only popup (sample)").count() == 0:
        db.add(Popup(title="Image-only popup (sample)", status="INACTIVE", target_audience="ALL", priority=1,
                     display_mode="image", image_url=_img + "&w=1080&h=1350", button_link="/pricing"))
    if db.query(OfferBanner).filter(OfferBanner.title == "Image-only banner (sample)").count() == 0:
        db.add(OfferBanner(title="Image-only banner (sample)", is_active=False, display_mode="image",
                           image_url=_img + "&w=1920&h=300", mobile_image_url=_img + "&w=800&h=400",
                           button_link="/pricing"))
    import whats_new as _wn
    _wn.import_defaults(db)                   # What's New modal: start with the built-in content (no-op if present)
    if db.query(Popup).filter(Popup.title == "Christmas Sale").count() == 0:
        db.add(Popup(
            title="Christmas Sale", status="ACTIVE", target_audience="ALL", priority=30, layout="stack",
            header_text="MERRY CHRISTMAS", emoji="\U0001F385", theme="snow", font_style="elegant", show_countdown=True,
            description="Wrap up the season with 3D motion videos. Unlock up to 50% off every plan.",
            coupon_code="XMAS50", button_text="Claim my gift", button_link="/pricing",
            footnote="*Offer valid for a limited time.",
            bg_color="#7f1d1d", bg_color2="#14532d", text_color="#ffffff", accent_color="#fde68a", button_color="#dc2626",
            start_at=datetime.utcnow() - timedelta(hours=1), end_at=datetime.utcnow() + timedelta(days=4)))
    if db.query(Popup).filter(Popup.title == "SAVE UP TO 75% OFF").count() == 0:
        db.add(Popup(
            title="SAVE UP TO 75% OFF", status="ACTIVE", target_audience="ALL", priority=20, layout="stack",
            header_text="WAIT! BONUS SPECIAL OFFER!", show_countdown=True,
            description="***LIMITED TIME DEAL***\nThis offer happens once a year, so take action TODAY before it goes away!",
            button_text="CLAIM MY DISCOUNT!", button_link="/pricing", footnote="*Discount is automatically applied to your purchase.",
            bg_color="#0b1c33", text_color="#ffffff", accent_color="#facc15", button_color="#6d5df6",
            start_at=datetime.utcnow() - timedelta(days=1), end_at=datetime.utcnow() + timedelta(hours=26)))
    if db.query(Popup).filter(Popup.title == "Summer Sale: 30% OFF").count() == 0:
        db.add(Popup(
            title="Summer Sale: 30% OFF", status="ACTIVE", target_audience="ALL", priority=5,
            bg_color="#ffffff", text_color="#111111", accent_color="#8a6d12",
            description="Turn your photos into stunning 3D motion videos.\nUpgrade this week and save on every plan.",
            image_url="https://images.unsplash.com/photo-1470071459604-3b5ec3a7fe05?auto=format&fit=crop&w=800&q=80",
            button_text="Upgrade now", button_link="/pricing", button_color="#c9a93b", coupon_code="SUMMER30",
            start_at=datetime.utcnow() - timedelta(days=1), end_at=datetime.utcnow() + timedelta(days=30)))
        db.add(Popup(
            title="Welcome to DepthFlow", status="INACTIVE", target_audience="FREE", priority=1,
            description="Start with a free render and explore before upgrading.",
            button_text="See plans", button_link="/pricing", button_color="#7c3aed"))
    if db.query(OfferBanner).filter(OfferBanner.title == "Christmas Sale").count() == 0:
        db.add(OfferBanner(
            title="Christmas Sale", is_active=True, header_text="MERRY CHRISTMAS", badge_text="UP TO 50% OFF",
            emoji="\U0001F384", theme="lights", font_style="elegant",
            description="Give the gift of 3D motion. Save on every plan this festive season.",
            coupon_code="XMAS50", button_text="Shop plans", button_link="/pricing", show_countdown=True,
            bg_color="#7f1d1d", bg_color2="#14532d", text_color="#ffffff", accent_color="#fde68a", button_color="#dc2626",
            start_at=datetime.utcnow() - timedelta(minutes=5), end_at=datetime.utcnow() + timedelta(days=4)))
    if db.query(OfferBanner).filter(OfferBanner.title == "Black Friday Mega Sale").count() == 0:
        db.add(OfferBanner(
            title="Black Friday Mega Sale", is_active=True, header_text="END OF SEASON", badge_text="50% OFF",
            description="Upgrade any plan and turn more photos into 3D motion videos. Offer ends soon.",
            coupon_code="BLACKFRIDAY", button_text="Shop plans", button_link="/pricing", show_countdown=True,
            bg_color="#120d2b", text_color="#ffffff", accent_color="#facc15", button_color="#7c3aed",
            start_at=datetime.utcnow() - timedelta(hours=1), end_at=datetime.utcnow() + timedelta(days=3)))
    if db.query(OfferBanner).count() == 0:
        db.add(OfferBanner(
            title="Summer Sale: 30% OFF all plans", is_active=True, coupon_code="SUMMER30",
            description="Use the code at checkout. Ends in 30 days.",
            button_text="View plans", button_link="/pricing",
            start_at=datetime.utcnow() - timedelta(days=1), end_at=datetime.utcnow() + timedelta(days=30)))
    db.commit()
    for email, kw in {
        "demo_admin@example.com": dict(is_admin=True, plan="free"),
        "demo_free@example.com": dict(plan="free"),
        "demo_paid@example.com": dict(plan="Pro", billing_cycle="monthly", subscription_status="active",
                                      subscription_id="sub_demo"),
    }.items():
        u = db.query(User).filter(User.email == email).first()
        if u is None:
            u = User(email=email, full_name=email.split("@")[0])
            db.add(u)
        u.hashed_password = get_password_hash("Demo@12345")
        u.is_verified = True
        u.credits = 100
        for k, v in kw.items():
            setattr(u, k, v)
    db.commit()
finally:
    db.close()

print("\nAdmin demo backend: http://localhost:%d   (fake payment gateway, temp DB: %s)" % (PORT, DB))
print("Logins (password Demo@12345): demo_admin@example.com | demo_free@example.com | demo_paid@example.com\n", flush=True)
uvicorn.run(main.app, host="127.0.0.1", port=PORT, log_level="warning")
