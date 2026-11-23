"""
Price-sync rehearsal: for each plan, raise the price by $0.10, verify it in Dodo, then revert it and verify again.

It goes through the REAL admin endpoints and the REAL Dodo API, but against a SCRATCH database (a temp file), so the
production database and the live site are never touched. The only lasting effect is new products in the Dodo account
selected by DODO_ENVIRONMENT (two per plan: the +$0.10 one and the reverted one). Re-running is cheap: products are found
again by their metadata `sync_key` instead of being duplicated.

  python scripts/price_cycle_check.py --dry-run                     # read-only: can Dodo see every plan product?
  python scripts/price_cycle_check.py --env test_mode --plan "Basic/monthly"
  python scripts/price_cycle_check.py --env live_mode --confirm-live --plan "Basic/monthly"   # start with ONE plan
  python scripts/price_cycle_check.py --env live_mode --confirm-live --all                    # then the rest

Needs DODO_PAYMENTS_API_KEY for the chosen environment (a test key only works with test_mode, a live key with live_mode).
Do NOT archive the products it creates in Dodo: if the same plan/version/price is used again the app reuses them.

Exit codes: 0 all good | 1 a check failed | 2 refused / nothing was tested.
"""
import argparse
import json
import os
import sys
import tempfile
import time
import warnings

warnings.filterwarnings("ignore")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", choices=["live_mode", "test_mode"], help="overrides DODO_ENVIRONMENT for this run")
    ap.add_argument("--plan", help='one plan, written "Name/interval", e.g. "Basic/monthly" (default when no --all)')
    ap.add_argument("--all", action="store_true", help="every importable plan")
    ap.add_argument("--dry-run", action="store_true", help="read-only pre-flight, creates nothing in Dodo")
    ap.add_argument("--confirm-live", action="store_true", help="required to create products in live_mode")
    ap.add_argument("--rehearsal-products", action="store_true",
                    help="test_mode only: use the DepthFlow Basic/Pro products that exist in the test account")
    return ap.parse_args()


args = parse_args()
os.chdir(ROOT)
sys.path.insert(0, ROOT)
if args.env:
    os.environ["DODO_ENVIRONMENT"] = args.env

from dotenv import load_dotenv  # noqa: E402

load_dotenv()  # does not override variables that are already set
ENV = os.environ.get("DODO_ENVIRONMENT", "live_mode").strip()
if ENV not in ("live_mode", "test_mode"):
    print(f"DODO_ENVIRONMENT must be live_mode or test_mode, got {ENV!r}")
    sys.exit(2)

print(f"Dodo environment: {ENV}")
if ENV == "live_mode" and not args.dry_run and not args.confirm_live:
    print("REFUSED: this run creates real products in your LIVE Dodo account (two per plan).\n"
          "Re-run with --confirm-live if that is what you want, or use --dry-run first (read-only).")
    sys.exit(2)
if args.rehearsal_products and ENV != "test_mode":
    print("REFUSED: --rehearsal-products only works in test_mode.")
    sys.exit(2)

# Scratch database: set BEFORE the app is imported so it can never point at the real one.
DB = os.path.join(tempfile.gettempdir(), "price_cycle_check.db")
if os.path.exists(DB):
    os.remove(DB)
os.environ["DATABASE_URL"] = "sqlite:///" + DB.replace("\\", "/")

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from auth import get_password_hash  # noqa: E402
from database import SessionLocal, engine  # noqa: E402
from models import User, PlanPrice, PricingPlan  # noqa: E402
from pricing_gateway import get_gateway  # noqa: E402
from routers import payments_router  # noqa: E402

assert str(engine.url).replace("\\", "/").endswith("price_cycle_check.db"), f"refusing to run on {engine.url}"
assert payments_router.DODO_ENVIRONMENT == ENV, "Dodo client environment does not match the requested one"
print("Database: scratch file (production is not used)\n")

db = SessionLocal()
db.add(User(email="cycle_admin@example.com", full_name="cycle", hashed_password=get_password_hash("Cycle@12345"),
            is_verified=True, is_admin=True, plan="free", credits=0))
db.commit()
db.close()

http = TestClient(main.app)
tok = http.post("/auth/login", json={"email": "cycle_admin@example.com", "password": "Cycle@12345"}).json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}
dodo = get_gateway().client

LEDGER = os.path.join(tempfile.gettempdir(), "price_cycle_ledger.json")
ledger = []


def save(entry):
    ledger.append(entry)
    with open(LEDGER, "w") as f:
        json.dump(ledger, f, indent=2)


def strip(d):
    skip = {"product_id", "name", "metadata", "created_at", "updated_at", "business_id", "price_detail"}
    return {k: v for k, v in d.items() if k not in skip}


def diff(a, b, path=""):
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            out += diff(a.get(k), b.get(k), f"{path}.{k}" if path else k)
    elif a != b:
        out.append(f"{path}: {a!r} -> {b!r}")
    return out


def benign(line):
    # Dodo returns '' for an empty activation message on old products and null on new ones; they mean the same thing.
    return line.startswith("price.price:") or line in ("license_key_activation_message: '' -> None",
                                                       "license_key_activation_message: None -> ''")


# ---------------------------------------------------------------------------------------------- pre-flight
if args.rehearsal_products:
    from datetime import datetime
    wanted = {"DepthFlow AI Basic": ("Basic", "monthly"), "DepthFlow AI Pro": ("Pro", "monthly"),
              "DepthFlow AI Basic yearly": ("Basic", "yearly"), "DepthFlow AI Pro yearly": ("Pro", "yearly")}
    found = {}
    for it in dodo.products.list(page_size=100):
        if it.name in wanted and it.name not in found:
            found[it.name] = it
    if len(found) != len(wanted):
        print("Missing test products:", set(wanted) - set(found))
        sys.exit(2)
    s = SessionLocal()
    for name, (key, interval) in wanted.items():
        prod = dodo.products.retrieve(found[name].product_id)
        pl = PricingPlan(plan_key=key, kind="workspace", billing_interval=interval, name=key, currency="USD", credits=100,
                         is_visible=True, is_active=True, pricing_version=1)
        s.add(pl)
        s.flush()
        s.add(PlanPrice(plan_id=pl.id, version=1, price_minor=int(prod.price.price), currency=str(prod.price.currency),
                        credits=100, gateway_product_id=prod.product_id, status="ACTIVE", activated_at=datetime.utcnow()))
    s.commit()
    s.close()
    print("Registered the 4 DepthFlow test products as plans in the scratch database.")
else:
    r = http.post("/admin/pricing-plans/import-from-dodo", headers=H)  # read-only against Dodo
    body = r.json() if r.status_code == 200 else {}
    print(f"Pre-flight (read-only): imported {len(body.get('imported', []))} plan(s) from Dodo")
    for line in body.get("errors", []):
        print("  not imported:", line)
    if r.status_code != 200:
        print("Import failed:", r.status_code, r.text[:300])
        sys.exit(2)

plans = http.get("/admin/pricing-plans", headers=H).json()["plans"]
plans = [p for p in plans if p["active_price"] and p["active_price"]["gateway_product_id"]]
label_of = lambda p: f"{p['plan_key']}/{p['billing_interval']}"  # noqa: E731
print("Plans Dodo can see:")
for p in plans:
    print(f"  {label_of(p):22} {p['active_price']['price_minor'] / 100:>9.2f} {p['active_price']['currency']}"
          f"   {p['active_price']['gateway_product_id']}")

if args.all:
    pass
else:
    pick = args.plan or "Basic/monthly"
    plans = [p for p in plans if label_of(p).lower() == pick.lower()]
if not plans:
    print("\nNothing to test: no matching plan could be read from Dodo (wrong API key for this environment?).")
    sys.exit(2)
if args.dry_run:
    print(f"\nDry run finished: {len(plans)} plan(s) would be tested. Nothing was created.")
    sys.exit(0)
print(f"\nRunning the +$0.10 / revert cycle on {len(plans)} plan(s) in {ENV} ...\n")


# ---------------------------------------------------------------------------------------------- the cycle
def prices_in_db(plan_id):
    s = SessionLocal()
    try:
        rows = s.query(PlanPrice).filter(PlanPrice.plan_id == plan_id).order_by(PlanPrice.version).all()
        return [(x.version, x.price_minor, x.status) for x in rows]
    finally:
        s.close()


def public_price(p):
    for x in http.get(f"/pricing-plans?kind={p['kind']}").json()["plans"]:
        if x["plan_key"] == p["plan_key"] and x["billing_interval"] == p["billing_interval"]:
            return x["price_minor"]
    return None


def put_price(p, minor):
    r = http.put(f"/admin/pricing-plans/{p['id']}", json={"price_minor": minor}, headers=H)
    return r, (r.json() if r.status_code == 200 else None)


failures = []
for p in plans:
    label = label_of(p)
    orig, orig_pid = p["active_price"]["price_minor"], p["active_price"]["gateway_product_id"]
    entry = {"plan": label, "currency": p["active_price"]["currency"], "original_minor": orig, "original_product": orig_pid}
    step = "increase"
    try:
        r, up = put_price(p, orig + 10)
        assert r.status_code == 200, f"PUT failed {r.status_code}: {r.text[:300]}"
        assert up["active_price"]["price_minor"] == orig + 10 and up["sync_status"] == "SYNCED", up
        pid2 = up["active_price"]["gateway_product_id"]
        assert pid2 and pid2 != orig_pid, "the price change must create a NEW product"
        new = dodo.products.retrieve(pid2)
        assert int(new.price.price) == orig + 10, f"Dodo shows {new.price.price}"
        old = dodo.products.retrieve(orig_pid)
        assert int(old.price.price) == orig, "the ORIGINAL product's price changed!"
        diffs = [d for d in diff(strip(old.model_dump(mode="json")), strip(new.model_dump(mode="json"))) if not benign(d)]
        assert not diffs, f"settings differ from the original product: {diffs}"
        assert public_price(p) == orig + 10, "/pricing-plans does not show the new price"
        entry.update(increased_minor=orig + 10, increased_product=pid2)
        print(f"[{label}] +0.10 ok: {orig} -> {orig + 10}, new product {pid2}; original untouched; settings identical")

        step = "revert"
        r, rv = put_price(p, orig)
        assert r.status_code == 200, f"revert failed {r.status_code}: {r.text[:300]}"
        assert rv["active_price"]["price_minor"] == orig and rv["sync_status"] == "SYNCED", rv
        pid3 = rv["active_price"]["gateway_product_id"]
        assert int(dodo.products.retrieve(pid3).price.price) == orig, "reverted product has the wrong price"
        assert int(dodo.products.retrieve(orig_pid).price.price) == orig, "the original product changed after the revert"
        assert int(dodo.products.retrieve(pid2).price.price) == orig + 10, "the +0.10 product changed"
        assert public_price(p) == orig, "public price not reverted"
        rows = prices_in_db(p["id"])
        assert [x[2] for x in rows] == ["RETIRED", "RETIRED", "ACTIVE"], rows
        entry.update(reverted_minor=orig, reverted_product=pid3, versions=rows, ok=True)
        print(f"[{label}] revert ok: back to {orig}, product {pid3}; history {rows}\n")
    except Exception as e:  # noqa: BLE001
        entry.update(ok=False, failed_at=step, error=str(e)[:500])
        failures.append(label)
        print(f"[{label}] FAILED at {step}: {e}")
        try:  # leave the scratch plan at its original price
            put_price(p, orig)
        except Exception:  # noqa: BLE001
            pass
        save(entry)
        print("Stopping at the first failure so nothing else is changed.")
        break
    save(entry)
    time.sleep(0.5)

created = [e.get(k) for e in ledger for k in ("increased_product", "reverted_product") if e.get(k)]
print(f"Products created in Dodo ({ENV}): {len(created)}  (ledger: {LEDGER})")
for pid in created:
    print("  ", pid)
print("Do not archive them; the app reuses them if the same plan/version/price is used again.")
print("\nRESULT:", f"ALL OK ({len(plans)} plan(s) cycled)" if not failures else f"FAILED: {failures}")
sys.exit(1 if failures else 0)
