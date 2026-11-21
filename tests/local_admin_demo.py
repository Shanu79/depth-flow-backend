"""
Run the admin features (pricing, popups, offer banner) locally WITHOUT touching real Dodo or your real database.

    python tests/local_admin_demo.py            # keeps the demo data between runs
    python tests/local_admin_demo.py --reset    # start from an empty demo database

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

DB = os.path.join(tempfile.gettempdir(), "depthflow_admin_demo.db")
if "--reset" in sys.argv and os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_URL"] = "sqlite:///" + DB.replace("\\", "/")       # never your real DB
os.environ["BASE_URL"] = "http://localhost:8010"

import uvicorn
from fastapi import APIRouter

import main
from auth import get_password_hash
from database import SessionLocal
from models import User, PopupDisplay
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

db = SessionLocal()
try:
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

print("\nAdmin demo backend: http://localhost:8010   (fake payment gateway, temp DB: %s)" % DB)
print("Logins (password Demo@12345): demo_admin@example.com | demo_free@example.com | demo_paid@example.com\n", flush=True)
uvicorn.run(main.app, host="127.0.0.1", port=8010, log_level="warning")
