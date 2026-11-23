"""
Admin-managed pricing plans, home popups and offer banner: tests.   Run:  python tests/test_pricing_popups.py

Safety: temp SQLite files + a FAKE payment gateway. Never touches your database and never calls Dodo.
No pytest needed (plain asserts + a tiny runner).
"""
import os, sys, shutil, tempfile, threading, itertools, traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ["DATABASE_URL"] = "sqlite://"     # before importing the app (dotenv will not override it)

from datetime import datetime, timedelta
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from database import Base, get_db
from models import User, Popup, PopupDisplay, OfferBanner, PricingPlan, PlanPrice
import auth
import content_service as cs
import content_validation as cv
import pricing
from pricing_gateway import GatewayError

TMP = tempfile.mkdtemp(prefix="pricing_tests_")
_counter = itertools.count()
_engines = []


def new_db(immediate=False):
    path = os.path.join(TMP, f"t{next(_counter)}.db")
    eng = create_engine(f"sqlite:///{path}", connect_args={"timeout": 60, "check_same_thread": False})
    if immediate:
        @event.listens_for(eng, "connect")
        def _connect(dbapi_conn, _rec):
            dbapi_conn.isolation_level = None

        @event.listens_for(eng, "begin")
        def _begin(conn):
            conn.exec_driver_sql("BEGIN IMMEDIATE")
    Base.metadata.create_all(eng)
    _engines.append(eng)
    return sessionmaker(bind=eng, autocommit=False, autoflush=False)


def make_user(db, email, plan="free", status="inactive", api_plan="free", api_status="inactive", admin=False):
    u = User(email=email, plan=plan, subscription_status=status, api_plan=api_plan,
             api_subscription_status=api_status, is_verified=True, is_admin=admin, credits=50)
    db.add(u); db.commit(); db.refresh(u)
    return u


NOW = datetime(2026, 6, 15, 12, 0, 0)


def make_popup(db, **kw):
    d = dict(title="Sale", description="d", target_audience="ALL", status="ACTIVE", priority=0,
             start_at=NOW - timedelta(hours=1), end_at=NOW + timedelta(days=1))
    d.update(kw)
    p = Popup(**d)
    db.add(p); db.commit(); db.refresh(p)
    return p


# ----------------------------------------------------------------------------------------------------------
# Fake payment gateway (same two methods as pricing_gateway.DodoGateway)
# ----------------------------------------------------------------------------------------------------------
LEGACY_PRICES = {            # what the "live" Dodo account would return for the legacy product ids
    "pdt_0NUQxXnGKlkhrpGBAFMvy": 1000, "pdt_0NUQxXJa0TR6vJJznsrt2": 2000, "pdt_0Nc5Ts7erauRnehAFJV6Q": 4000,
    "pdt_0NUQxWNFaRWTd8eoNkqoJ": 10800, "pdt_0NUQxVyW088PIAwIzObuC": 21600, "pdt_0Nc5TfN5t1oLCfoeNN7AZ": 43200,
    "pdt_0Nbt4jmPtgBIPmJgF3qdh": 4900, "pdt_0NbtLLrNFAwrGkvpH0lGQ": 9900, "pdt_0Nbt4bIY0qlHH3u8SMyyD": 19900,
    "pdt_0NYCtBgPqWWzyF6yKrE98": 1500,
}


class FakeGateway:
    def __init__(self):
        self.products = {pid: {"price_minor": p, "currency": "USD", "sync_key": None}
                         for pid, p in LEGACY_PRICES.items()}
        self.create_calls = 0
        self.fail_next = None            # "before" | "after_create"
        self.specs = []

    def read_product(self, product_id):
        p = self.products.get(product_id)
        if p is None:
            raise GatewayError(f"Could not read Dodo product {product_id}: 404")
        return {"price_minor": p["price_minor"], "currency": p["currency"]}

    def ensure_product(self, sync_key, spec):
        self.specs.append(spec)
        if self.fail_next == "before":
            self.fail_next = None
            raise GatewayError("Dodo is unreachable")
        pid = next((k for k, v in self.products.items() if v["sync_key"] == sync_key), None)
        if pid is None:
            self.create_calls += 1
            pid = f"pdt_new_{self.create_calls}"
            self.products[pid] = {"price_minor": spec["price_minor"], "currency": spec["currency"],
                                  "sync_key": sync_key}
        if self.fail_next == "after_create":
            self.fail_next = None
            raise GatewayError("verification timed out")
        return pid


def import_all(db, gw):
    return pricing.import_legacy_plans(db, gw, admin_id=None)


# ----------------------------------------------------------------------------------------------------------
# HTTP environment
# ----------------------------------------------------------------------------------------------------------
def http_env(gateway=None):
    from fastapi.testclient import TestClient
    import main
    from routers import pricing_router
    S = new_db()
    state = {"email": None}

    def _db():
        d = S()
        try:
            yield d
        finally:
            d.close()

    async def _user():
        d = S()
        try:
            u = d.query(User).filter(User.email == state["email"]).first()
            if u is None:
                from fastapi import HTTPException
                raise HTTPException(status_code=401, detail="no user")
            d.expunge(u)
            return u
        finally:
            d.close()

    main.app.dependency_overrides[get_db] = _db
    main.app.dependency_overrides[auth.get_current_user] = _user
    gw = gateway or FakeGateway()
    main.app.dependency_overrides[pricing_router.get_gateway] = lambda: gw
    client = TestClient(main.app)

    def cleanup():
        main.app.dependency_overrides.clear()

    return client, S, state, gw, cleanup


def with_admin(S, state, email="admin@x.com"):
    d = S()
    make_user(d, email, admin=True)
    d.close()
    state["email"] = email


def popup_body(**kw):
    b = {"title": "Big sale", "description": "50% off", "button_text": "Go", "button_link": "https://depthflow.io/pricing",
         "coupon_code": "save50", "target_audience": "ALL", "status": "ACTIVE", "priority": 1}
    b.update(kw)
    return b


# ==========================================================================================================
# POPUPS
# ==========================================================================================================
def test_01_admin_can_create_edit_activate_archive_popup():
    c, S, st, gw, done = http_env()
    try:
        with_admin(S, st)
        r = c.post("/admin/popups", json=popup_body(status="INACTIVE"))
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        assert r.json()["coupon_code"] == "SAVE50" and r.json()["state"] == "INACTIVE"
        r = c.put(f"/admin/popups/{pid}", json=popup_body(title="Renamed", status="INACTIVE"))
        assert r.status_code == 200 and r.json()["title"] == "Renamed"
        r = c.patch(f"/admin/popups/{pid}/status", json={"status": "ACTIVE"})
        assert r.json()["state"] == "LIVE"
        r = c.delete(f"/admin/popups/{pid}")
        assert r.json()["status"] == "archived"
        assert c.get("/admin/popups").json()["popups"] == []                       # archived hidden by default
        assert len(c.get("/admin/popups?include_archived=true").json()["popups"]) == 1
        assert c.put(f"/admin/popups/{pid}", json=popup_body()).status_code == 409   # archived: not editable
    finally:
        done()


def test_02_only_admins_can_manage_popups_pricing_and_banner():
    c, S, st, gw, done = http_env()
    try:
        d = S(); make_user(d, "user@x.com"); d.close()
        st["email"] = "user@x.com"
        calls = [("get", "/admin/popups", None), ("post", "/admin/popups", popup_body()),
                 ("get", "/admin/pricing-plans", None), ("post", "/admin/pricing-plans/import-from-dodo", None),
                 ("get", "/admin/offer-banner", None), ("post", "/admin/offer-banner", {"title": "x"})]
        for method, url, body in calls:
            r = getattr(c, method)(url, json=body) if body is not None else getattr(c, method)(url)
            assert r.status_code == 403, (method, url, r.status_code)
    finally:
        done()


def test_03_popup_respects_date_window_and_status():
    S = new_db(); db = S()
    u = make_user(db, "a@x.com")
    make_popup(db, title="future", start_at=NOW + timedelta(hours=2), end_at=NOW + timedelta(days=2))
    make_popup(db, title="expired", start_at=NOW - timedelta(days=2), end_at=NOW - timedelta(hours=1))
    make_popup(db, title="inactive", status="INACTIVE")
    make_popup(db, title="archived", status="ARCHIVED")
    live = make_popup(db, title="live")
    assert [p.title for p in cs.eligible_popups(db, u, limit=5, now=NOW)] == ["live"]
    # it expires automatically, and the future one starts automatically
    assert [p.title for p in cs.eligible_popups(db, u, limit=5, now=NOW + timedelta(hours=3))] == ["future", "live"] \
        or [p.title for p in cs.eligible_popups(db, u, limit=5, now=NOW + timedelta(hours=3))] == ["live", "future"]
    assert cs.eligible_popups(db, u, limit=5, now=live.end_at + timedelta(seconds=1)) == \
        [p for p in cs.eligible_popups(db, u, limit=5, now=live.end_at + timedelta(seconds=1)) if p.title != "live"]
    # boundaries are inclusive
    assert "live" in [p.title for p in cs.eligible_popups(db, u, limit=5, now=live.end_at)]
    assert "live" in [p.title for p in cs.eligible_popups(db, u, limit=5, now=live.start_at)]


def test_04_audience_targeting_is_enforced_on_the_backend():
    S = new_db(); db = S()
    free = make_user(db, "free@x.com")
    credit_pack = make_user(db, "pack@x.com", plan="Credit Pack", status="inactive")
    cancelled = make_user(db, "cancel@x.com", plan="Pro", status="cancelled")
    paid = make_user(db, "paid@x.com", plan="Pro", status="active")
    scheduled = make_user(db, "sched@x.com", plan="Basic", status="Scheduled for cancellation on 2026-07-01")
    api_paid = make_user(db, "api@x.com", api_plan="Pro API", api_status="active")
    make_popup(db, title="all", target_audience="ALL")
    make_popup(db, title="free", target_audience="FREE")
    make_popup(db, title="paid", target_audience="PAID")

    def titles(u):
        return sorted(p.title for p in cs.eligible_popups(db, u, limit=5, now=NOW))
    assert titles(free) == ["all", "free"]
    assert titles(credit_pack) == ["all", "free"]          # credit pack != paid subscription
    assert titles(cancelled) == ["all", "free"]
    assert titles(paid) == ["all", "paid"]
    assert titles(scheduled) == ["all", "paid"]
    assert titles(api_paid) == ["all", "paid"]


def test_05_popup_shown_once_per_user_per_day_and_again_next_day():
    S = new_db(); db = S()
    u = make_user(db, "a@x.com"); other = make_user(db, "b@x.com")
    p = make_popup(db, end_at=NOW + timedelta(days=5))
    assert [x.id for x in cs.eligible_popups(db, u, now=NOW)] == [p.id]
    assert cs.track_display(db, u, p.id, now=NOW) is True
    assert cs.track_display(db, u, p.id, now=NOW + timedelta(minutes=5)) is False        # same day: idempotent
    assert db.query(PopupDisplay).count() == 1
    assert cs.eligible_popups(db, u, now=NOW + timedelta(hours=1)) == []                 # refresh -> nothing
    assert [x.id for x in cs.eligible_popups(db, other, now=NOW)] == [p.id]              # per-user
    assert [x.id for x in cs.eligible_popups(db, u, now=NOW + timedelta(days=1))] == [p.id]  # next UTC day
    assert cs.track_display(db, u, p.id, now=NOW + timedelta(days=1)) is True
    assert db.query(PopupDisplay).count() == 2


def test_06_db_blocks_duplicate_display_rows_and_concurrent_tracking():
    S = new_db(immediate=True); db = S()
    u = make_user(db, "a@x.com")
    p = make_popup(db, end_at=NOW + timedelta(days=5))
    db.add(PopupDisplay(popup_id=p.id, user_id=u.id, displayed_at=NOW, display_date=NOW.date())); db.commit()
    db.add(PopupDisplay(popup_id=p.id, user_id=u.id, displayed_at=NOW, display_date=NOW.date()))
    try:
        db.commit(); raise AssertionError("duplicate display row was accepted")
    except IntegrityError:
        db.rollback()
    # concurrent track calls for a fresh day: exactly one row
    results = []
    uid, popup_id = u.id, p.id
    db.commit()
    def worker():
        s = S()
        try:
            user = s.get(User, uid)
            results.append(cs.track_display(s, user, popup_id, now=NOW + timedelta(days=1)))
        finally:
            s.close()
    db.commit()
    ts = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(results, key=str).count(True) == 1, results
    assert db.query(PopupDisplay).filter(PopupDisplay.display_date == (NOW + timedelta(days=1)).date()).count() == 1


def test_07_multiple_popups_follow_priority_and_limit():
    S = new_db(); db = S()
    u = make_user(db, "a@x.com")
    make_popup(db, title="low", priority=1)
    make_popup(db, title="high", priority=9)
    make_popup(db, title="mid", priority=5)
    assert [p.title for p in cs.eligible_popups(db, u, limit=1, now=NOW)] == ["high"]
    assert [p.title for p in cs.eligible_popups(db, u, limit=3, now=NOW)] == ["high", "mid", "low"]


def test_08_http_eligible_and_track_flow_and_ineligible_track_is_404():
    c, S, st, gw, done = http_env()
    try:
        with_admin(S, st)
        pid = c.post("/admin/popups", json=popup_body(target_audience="FREE")).json()["id"]
        paid_only = c.post("/admin/popups", json=popup_body(target_audience="PAID", title="paid only")).json()["id"]
        d = S(); make_user(d, "free@x.com"); d.close()
        st["email"] = "free@x.com"
        r = c.get("/popups/eligible"); assert r.status_code == 200, r.text
        assert [p["id"] for p in r.json()["popups"]] == [pid]
        assert "target_audience" not in r.json()["popups"][0]
        assert c.post(f"/popups/{pid}/track").json()["status"] == "recorded"
        assert c.post(f"/popups/{pid}/track").json()["status"] == "already_tracked"
        assert c.get("/popups/eligible").json()["popups"] == []                        # refresh / navigation
        assert c.post(f"/popups/{paid_only}/track").status_code == 404                 # not my audience
        assert c.post("/popups/99999/track").status_code == 404
    finally:
        done()


def test_09_popup_and_banner_input_is_sanitised_and_validated():
    c, S, st, gw, done = http_env()
    try:
        with_admin(S, st)
        r = c.post("/admin/popups", json=popup_body(title="<script>alert(1)</script>Hello <b>World</b>",
                                                    description="<img src=x onerror=alert(1)>text"))
        assert r.status_code == 201, r.text
        assert r.json()["title"] == "alert(1)Hello World" and r.json()["description"] == "text"
        for bad_link in ("javascript:alert(1)", "data:text/html,hi", "//evil.com", "http://insecure.com", "ftp://x.com"):
            assert c.post("/admin/popups", json=popup_body(button_link=bad_link)).status_code == 422, bad_link
        assert c.post("/admin/popups", json=popup_body(button_link="/pricing")).status_code == 201   # in-app path ok
        assert c.post("/admin/popups", json=popup_body(coupon_code="bad code!")).status_code == 422
        assert c.post("/admin/popups", json=popup_body(image_url="javascript:alert(1)")).status_code == 422
        assert c.post("/admin/popups", json=popup_body(target_audience="EVERYONE")).status_code == 422
        assert c.post("/admin/popups", json=popup_body(title="   ")).status_code == 422
        r = c.post("/admin/popups", json=popup_body(button_color="#C9A93B"))
        assert r.status_code == 201 and r.json()["button_color"] == "#c9a93b", r.text          # normalised
        assert c.post("/admin/popups", json=popup_body()).json()["button_color"] is None       # optional
        for bad_color in ("red", "#fff", "#12345g", "javascript:1", "#ff0000;x"):
            assert c.post("/admin/popups", json=popup_body(button_color=bad_color)).status_code == 422, bad_color
        for field in ("text_color", "bg_color", "accent_color"):
            assert c.post("/admin/popups", json=popup_body(**{field: "url(x)"})).status_code == 422, field
        r = c.post("/admin/popups", json=popup_body(
            layout="stack", header_text="<b>WAIT!</b> Offer", footnote="*terms", bg_color="#0B1C33",
            text_color="#ffffff", accent_color="#FACC15", show_countdown=True, end_at="2099-01-01T00:00:00Z"))
        j = r.json()
        assert r.status_code == 201, r.text
        assert (j["layout"], j["header_text"], j["footnote"], j["bg_color"], j["accent_color"], j["show_countdown"]) ==             ("stack", "WAIT! Offer", "*terms", "#0b1c33", "#facc15", True), j
        r = c.post("/admin/popups", json=popup_body(theme="snow", emoji="<b>🎅</b>", font_style="elegant",
                                                    bg_color="#7F1D1D", bg_color2="#14532D"))
        j = r.json()
        assert r.status_code == 201, r.text
        assert (j["theme"], j["emoji"], j["font_style"], j["bg_color2"]) == ("snow", "🎅", "elegant", "#14532d"), j
        d0 = c.post("/admin/popups", json=popup_body()).json()
        assert (d0["theme"], d0["font_style"], d0["emoji"], d0["bg_color2"]) == ("none", "modern", None, None)
        assert c.post("/admin/popups", json=popup_body(theme="fire")).status_code == 422
        assert c.post("/admin/popups", json=popup_body(font_style="comic")).status_code == 422
        assert c.post("/admin/popups", json=popup_body(bg_color2="blue")).status_code == 422
        assert c.post("/admin/popups", json=popup_body(emoji="x" * 9)).status_code == 422
        rb = c.post("/admin/offer-banner", json={"title": "X", "theme": "lights", "emoji": "🎄", "bg_color2": "#14532d"})
        assert rb.status_code == 201 and rb.json()["theme"] == "lights" and rb.json()["bg_color2"] == "#14532d", rb.text
        assert c.post("/admin/popups", json=popup_body(show_countdown=True)).status_code == 422   # countdown needs an end date
        assert c.post("/admin/popups", json=popup_body(layout="weird")).status_code == 422
        d = c.post("/admin/popups", json=popup_body()).json()
        assert d["layout"] == "split" and d["show_countdown"] is False and d["bg_color"] is None
        assert c.post("/admin/popups", json=popup_body(start_at="2026-06-02T10:00:00Z",
                                                       end_at="2026-06-01T10:00:00Z")).status_code == 422
        r = c.post("/admin/popups", json=popup_body(start_at="2026-06-02T10:00:00+02:00"))
        assert r.json()["start_at"] == "2026-06-02T08:00:00Z"                             # stored as UTC
        assert c.post("/admin/offer-banner", json={"title": "x", "button_link": "javascript:1",
                                                   "button_text": "y"}).status_code == 422
    finally:
        done()


def test_10_image_upload_validates_type_size_and_ignores_client_filename():
    c, S, st, gw, done = http_env()
    old_root = cv.UPLOAD_ROOT
    cv.UPLOAD_ROOT = os.path.join(TMP, "uploads")
    try:
        with_admin(S, st)
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 200
        r = c.post("/admin/uploads/image", files={"file": ("../../evil.php", png, "image/png")})
        assert r.status_code == 200, r.text
        name = r.json()["url"].rsplit("/", 1)[-1]
        assert name.endswith(".png") and "evil" not in name and os.path.exists(os.path.join(cv.UPLOAD_ROOT, name))
        svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
        assert c.post("/admin/uploads/image", files={"file": ("a.png", svg, "image/png")}).status_code == 400
        assert c.post("/admin/uploads/image", files={"file": ("a.png", b"GIF89a....", "image/png")}).status_code == 400
        assert c.post("/admin/uploads/image", files={"file": ("a.png", b"", "image/png")}).status_code == 400
        big = b"\xff\xd8\xff" + b"0" * (cv.MAX_IMAGE_BYTES + 10)
        assert c.post("/admin/uploads/image", files={"file": ("big.jpg", big, "image/jpeg")}).status_code == 413
        d = S(); make_user(d, "u@x.com"); d.close(); st["email"] = "u@x.com"
        assert c.post("/admin/uploads/image", files={"file": ("a.png", png, "image/png")}).status_code == 403
    finally:
        cv.UPLOAD_ROOT = old_root
        done()


# ==========================================================================================================
# OFFER BANNER
# ==========================================================================================================
def test_11_offer_banner_follows_schedule_and_toggle():
    S = new_db(); db = S()
    def mk(**kw):
        d = dict(title="b", is_active=True, start_at=None, end_at=None); d.update(kw)
        b = OfferBanner(**d); db.add(b); db.commit(); return b
    assert cs.active_banner(db, NOW) is None
    mk(title="disabled", is_active=False)
    mk(title="future", start_at=NOW + timedelta(hours=1))
    mk(title="expired", end_at=NOW - timedelta(seconds=1))
    assert cs.active_banner(db, NOW) is None
    live = mk(title="live", start_at=NOW - timedelta(days=1), end_at=NOW + timedelta(days=1))
    assert cs.active_banner(db, NOW).title == "live"
    # "future" has no end date and starts later, so once it starts it wins (most recently started)
    assert cs.active_banner(db, NOW + timedelta(hours=2)).title == "future"
    # live has expired by now; future keeps running
    assert cs.active_banner(db, NOW + timedelta(days=2)).title == "future"
    # toggling off removes it immediately
    for b in db.query(OfferBanner).all():
        b.is_active = False
    db.commit()
    assert cs.active_banner(db, NOW) is None


def test_12_http_banner_admin_and_public_endpoint():
    c, S, st, gw, done = http_env()
    try:
        with_admin(S, st)
        assert c.get("/offer-banner/active").json()["banner"] is None
        r = c.post("/admin/offer-banner", json={"title": "Black Friday", "description": "30% off", "button_text": "Buy",
                                                "button_link": "/pricing", "coupon_code": "bf30", "is_active": True})
        assert r.status_code == 201 and r.json()["state"] == "LIVE", r.text
        bid = r.json()["id"]
        # style fields on the banner
        r2 = c.post("/admin/offer-banner", json={"title": "BF", "badge_text": "<i>50% OFF</i>", "header_text": "END OF SEASON",
                                                  "bg_color": "#120D2B", "accent_color": "#facc15", "show_countdown": True,
                                                  "end_at": "2099-01-01T00:00:00Z"})
        assert r2.status_code == 201, r2.text
        j = r2.json()
        assert (j["badge_text"], j["header_text"], j["bg_color"], j["show_countdown"]) == ("50% OFF", "END OF SEASON", "#120d2b", True), j
        assert c.post("/admin/offer-banner", json={"title": "x", "show_countdown": True}).status_code == 422   # needs end date
        assert c.post("/admin/offer-banner", json={"title": "x", "bg_color": "red"}).status_code == 422
        c.delete(f"/admin/offer-banner/{j['id']}")
        st["email"] = None                                                    # public endpoint needs no login
        b = c.get("/offer-banner/active").json()["banner"]
        assert b["title"] == "Black Friday" and b["coupon_code"] == "BF30"
        st["email"] = "admin@x.com"
        c.patch(f"/admin/offer-banner/{bid}/status", json={"is_active": False})
        assert c.get("/offer-banner/active").json()["banner"] is None
        assert c.delete(f"/admin/offer-banner/{bid}").json()["status"] == "deleted"
    finally:
        done()


# ==========================================================================================================
# PRICING
# ==========================================================================================================
def test_13_legacy_fallback_before_import_keeps_checkout_working():
    S = new_db(); db = S()
    r = pricing.resolve_for_checkout(db, "Pro", "monthly")
    assert (r.product_id, r.credits, r.is_api, r.is_one_time) == ("pdt_0NUQxXJa0TR6vJJznsrt2", 1500, False, False)
    r = pricing.resolve_for_checkout(db, "Growth API", "monthly")
    assert r.is_api and r.credits == 30000
    r = pricing.resolve_for_checkout(db, "Credit Pack", "one_time")
    assert r.is_one_time and r.credits == 900
    for bad in (("Nope", "monthly"), ("Pro", "weekly"), ("Basic", "one_time")):
        try:
            pricing.resolve_for_checkout(db, *bad); raise AssertionError(f"{bad} should be invalid")
        except pricing.PlanUnavailable as e:
            assert e.status_code == 400
    assert pricing.resolve_by_product_id(db, "pdt_0NUQxWNFaRWTd8eoNkqoJ", api=False) == ("Basic", "yearly")
    assert pricing.resolve_by_product_id(db, "pdt_0Nbt4bIY0qlHH3u8SMyyD", api=True) == ("Pro API", "monthly")
    assert pricing.yearly_credits(db, "Pro", api=False) == 18000


def test_14_import_reads_real_prices_is_idempotent_and_skips_placeholders():
    S = new_db(); db = S(); gw = FakeGateway()
    res = import_all(db, gw)
    assert len(res["imported"]) == 10 and not [e for e in res["errors"] if "placeholder" not in e], res
    assert len([e for e in res["errors"] if "placeholder" in e]) == 3          # dummy_* API yearly ids
    p = pricing.get_plan(db, "Basic", "monthly")
    a = pricing.active_price(db, p.id)
    assert (a.price_minor, a.currency, a.credits, a.version) == (1000, "USD", 600, 1)
    again = import_all(db, gw)
    assert again["imported"] == [] and len(again["skipped"]) == 10
    assert db.query(PricingPlan).count() == 10 and db.query(PlanPrice).count() == 10
    # a product Dodo cannot return is skipped and keeps working through the legacy fallback
    S2 = new_db(); db2 = S2(); gw2 = FakeGateway(); del gw2.products["pdt_0NUQxXJa0TR6vJJznsrt2"]
    res2 = import_all(db2, gw2)
    assert any("Pro/monthly" in e for e in res2["errors"])
    assert pricing.resolve_for_checkout(db2, "Pro", "monthly").product_id == "pdt_0NUQxXJa0TR6vJJznsrt2"


def test_15_public_pricing_lists_only_active_visible_priced_plans_in_order():
    c, S, st, gw, done = http_env()
    try:
        with_admin(S, st)
        assert c.get("/pricing-plans").json()["plans"] == []                      # nothing imported yet
        assert c.post("/admin/pricing-plans/import-from-dodo").status_code == 200
        st["email"] = None                                                         # public: no login
        plans = c.get("/pricing-plans?kind=workspace").json()["plans"]
        assert len(plans) == 6 and all(p["kind"] == "workspace" for p in plans)
        assert "gateway_product_id" not in plans[0] and "pdt_" not in str(plans)
        assert c.get("/pricing-plans").json()["plans"].__len__() == 10             # + API monthly + credit pack
        st["email"] = "admin@x.com"
        ids = {(p["plan_key"], p["billing_interval"]): p["id"] for p in c.get("/admin/pricing-plans").json()["plans"]}
        c.patch(f"/admin/pricing-plans/{ids[('Pro', 'monthly')]}/status", json={"is_visible": False})
        c.patch(f"/admin/pricing-plans/{ids[('Basic', 'monthly')]}/status", json={"is_active": False})
        c.post("/admin/pricing-plans/reorder", json={"items": [{"id": ids[("Premium", "monthly")], "display_order": 0},
                                                                {"id": ids[("Pro", "yearly")], "display_order": 1}]})
        st["email"] = None
        m = [p["plan_key"] for p in c.get("/pricing-plans?kind=workspace").json()["plans"] if p["billing_interval"] == "monthly"]
        assert m == ["Premium"], m                                                  # Pro hidden, Basic inactive
    finally:
        done()


def test_16_admin_can_edit_text_features_credits_order_and_badge():
    c, S, st, gw, done = http_env()
    try:
        with_admin(S, st)
        c.post("/admin/pricing-plans/import-from-dodo")
        plans = {(p["plan_key"], p["billing_interval"]): p for p in c.get("/admin/pricing-plans").json()["plans"]}
        pid = plans[("Basic", "monthly")]["id"]
        r = c.put(f"/admin/pricing-plans/{pid}", json={
            "name": "Starter <b>Plus</b>", "description": "For <i>creators</i>", "credits": 700,
            "features": ["700 credits", "  HD <b>export</b> ", "", "No watermark"], "display_order": 3,
            "is_most_popular": True, "badge_text": "Best value"})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["name"] == "Starter Plus" and j["description"] == "For creators"
        assert j["features"] == ["700 credits", "HD export", "No watermark"]
        assert j["credits"] == 700 and j["active_price"]["credits"] == 700 and j["active_price"]["version"] == 1
        assert j["is_most_popular"] and j["badge_text"] == "Best value"
        assert gw.create_calls == 0                                              # display edits never touch the gateway
        # only one Most Popular per group: moving it clears the previous one
        pro = plans[("Pro", "monthly")]["id"]
        c.put(f"/admin/pricing-plans/{pro}", json={"is_most_popular": True})
        monthly = [p for p in c.get("/admin/pricing-plans").json()["plans"]
                   if p["kind"] == "workspace" and p["billing_interval"] == "monthly"]
        assert [p["plan_key"] for p in monthly if p["is_most_popular"]] == ["Pro"]
        assert [p["badge_text"] for p in monthly if p["is_most_popular"]] == ["Most Popular"]
        st["email"] = None
        pub = {p["plan_key"]: p for p in c.get("/pricing-plans?kind=workspace").json()["plans"] if p["billing_interval"] == "monthly"}
        assert pub["Pro"]["is_most_popular"] and pub["Pro"]["badge_text"] == "Most Popular"
        assert not pub["Basic"]["is_most_popular"] and pub["Basic"]["badge_text"] is None
        assert pub["Basic"]["name"] == "Starter Plus" and pub["Basic"]["credits"] == 700
        # plan_key (what users.plan / checkout use) is unchanged by renaming
        assert pub["Basic"]["plan_key"] == "Basic"
    finally:
        done()


def test_17_db_allows_only_one_most_popular_per_group():
    S = new_db(); db = S()
    def mk(key, popular):
        p = PricingPlan(plan_key=key, kind="workspace", billing_interval="monthly", name=key, credits=1,
                        is_most_popular=popular)
        db.add(p); db.commit()
    mk("A", True); mk("B", False)
    try:
        mk("C", True); raise AssertionError("second Most Popular accepted")
    except IntegrityError:
        db.rollback()


def test_18_price_change_publishes_new_version_and_keeps_old_product_mapped():
    c, S, st, gw, done = http_env()
    try:
        with_admin(S, st)
        c.post("/admin/pricing-plans/import-from-dodo")
        pid = next(p["id"] for p in c.get("/admin/pricing-plans").json()["plans"]
                   if (p["plan_key"], p["billing_interval"]) == ("Pro", "monthly"))
        r = c.put(f"/admin/pricing-plans/{pid}", json={"price_minor": 2500})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["sync_status"] == "SYNCED" and j["active_price"]["price_minor"] == 2500 and j["active_price"]["version"] == 2
        assert j["proposed_price"] is None and gw.create_calls == 1
        assert gw.specs[-1]["template_product_id"] == "pdt_0NUQxXJa0TR6vJJznsrt2"      # settings cloned from current
        new_pid = j["active_price"]["gateway_product_id"]
        assert new_pid == "pdt_new_1" and gw.products[new_pid]["price_minor"] == 2500
        d = S()
        rows = d.query(PlanPrice).filter(PlanPrice.plan_id == pid).order_by(PlanPrice.version).all()
        assert [(r_.version, r_.status) for r_ in rows] == [(1, "RETIRED"), (2, "ACTIVE")]
        assert rows[0].gateway_product_id == "pdt_0NUQxXJa0TR6vJJznsrt2" and rows[0].retired_at is not None
        # public price follows; old + new product ids both resolve to the same plan (existing subscribers safe)
        st["email"] = None
        pub = next(p for p in c.get("/pricing-plans").json()["plans"] if (p["plan_key"], p["billing_interval"]) == ("Pro", "monthly"))
        assert pub["price_minor"] == 2500 and pub["price"] == 25.0 and pub["pricing_version"] == 2
        assert pricing.resolve_by_product_id(d, "pdt_0NUQxXJa0TR6vJJznsrt2", api=False) == ("Pro", "monthly")
        assert pricing.resolve_by_product_id(d, new_pid, api=False) == ("Pro", "monthly")
        # same price again = no-op (no duplicate gateway product)
        st["email"] = "admin@x.com"
        c.put(f"/admin/pricing-plans/{pid}", json={"price_minor": 2500})
        assert gw.create_calls == 1
        d.close()
    finally:
        done()


def test_19_gateway_failure_keeps_old_price_then_retry_creates_exactly_one_product():
    c, S, st, gw, done = http_env()
    try:
        with_admin(S, st)
        c.post("/admin/pricing-plans/import-from-dodo")
        pid = next(p["id"] for p in c.get("/admin/pricing-plans").json()["plans"]
                   if (p["plan_key"], p["billing_interval"]) == ("Basic", "yearly"))
        # (a) gateway down before anything is created
        gw.fail_next = "before"
        r = c.put(f"/admin/pricing-plans/{pid}", json={"price_minor": 9999, "name": "Basic Yearly"})
        assert r.status_code == 502 and r.json()["code"] == "GATEWAY_SYNC_FAILED", r.text
        assert "Dodo is unreachable" in r.json()["detail"]
        plan = r.json()["plan"]
        assert plan["active_price"]["price_minor"] == 10800 and plan["active_price"]["version"] == 1
        assert plan["sync_status"] == "FAILED" and plan["proposed_price"]["price_minor"] == 9999
        assert plan["name"] == "Basic Yearly"                                      # display edit was still saved
        st["email"] = None
        assert next(p for p in c.get("/pricing-plans").json()["plans"]
                    if (p["plan_key"], p["billing_interval"]) == ("Basic", "yearly"))["price_minor"] == 10800
        st["email"] = "admin@x.com"
        d = S(); assert pricing.resolve_for_checkout(d, "Basic", "yearly").product_id == "pdt_0NUQxWNFaRWTd8eoNkqoJ"; d.close()
        # (b) product gets created but verification fails -> retry must reuse it
        gw.fail_next = "after_create"
        r = c.post(f"/admin/pricing-plans/{pid}/sync-payment-gateway")
        assert r.status_code == 502 and gw.create_calls == 1
        r = c.post(f"/admin/pricing-plans/{pid}/sync-payment-gateway")
        assert r.status_code == 200, r.text
        assert gw.create_calls == 1, "retry created a duplicate Dodo product"
        j = r.json()
        assert j["sync_status"] == "SYNCED" and j["active_price"]["price_minor"] == 9999 and j["active_price"]["version"] == 2
        # nothing left to sync
        assert c.post(f"/admin/pricing-plans/{pid}/sync-payment-gateway").status_code == 400
    finally:
        done()


def test_20_changing_the_proposed_price_supersedes_a_failed_one():
    S = new_db(); db = S(); gw = FakeGateway(); import_all(db, gw)
    plan = pricing.get_plan(db, "Premium", "monthly")
    rid = pricing.begin_price_change(db, plan.id, 5000, "USD", None)
    gw.fail_next = "before"
    try:
        pricing.sync_price(db, rid, gw); raise AssertionError("expected failure")
    except pricing.PricingSyncError:
        pass
    rid2 = pricing.begin_price_change(db, plan.id, 6000, "USD", None)         # admin corrects the price
    assert rid2 != rid
    pricing.sync_price(db, rid2, gw)
    rows = db.query(PlanPrice).filter(PlanPrice.plan_id == plan.id).order_by(PlanPrice.version).all()
    assert [(r.version, r.status) for r in rows] == [(1, "RETIRED"), (2, "FAILED"), (3, "ACTIVE")]
    assert pricing.active_price(db, plan.id).price_minor == 6000


def test_21_concurrent_price_edits_never_leave_inconsistent_pricing():
    S = new_db(immediate=True); db = S(); gw = FakeGateway(); import_all(db, gw)
    plan_id = pricing.get_plan(db, "Pro", "monthly").id
    db.commit()
    outcomes = []
    def worker(price):
        s = S()
        try:
            rid = pricing.begin_price_change(s, plan_id, price, "USD", None)
            if rid is not None:
                pricing.sync_price(s, rid, gw)
            outcomes.append("ok")
        except (pricing.PricingConflict, pricing.PricingSyncError) as e:
            outcomes.append(type(e).__name__)
        finally:
            s.close()
    ts = [threading.Thread(target=worker, args=(3000 + i * 100,)) for i in range(5)]
    [t.start() for t in ts]; [t.join() for t in ts]
    db.expire_all()
    rows = db.query(PlanPrice).filter(PlanPrice.plan_id == plan_id).all()
    assert sum(1 for r in rows if r.status == "ACTIVE") == 1, [(r.version, r.status) for r in rows]
    assert sum(1 for r in rows if r.status == "PENDING") <= 1
    act = next(r for r in rows if r.status == "ACTIVE")
    assert act.price_minor == gw.products[act.gateway_product_id]["price_minor"]     # app price == gateway price
    assert "ok" in outcomes


def test_22_checkout_uses_backend_price_ignores_frontend_and_blocks_inactive_plans():
    from routers import payments_router
    c, S, st, gw, done = http_env()
    captured = {}

    class _Customers:
        def list(self, email=None):
            return type("L", (), {"items": []})()

    class _Sessions:
        def create(self, **kw):
            captured.update(kw)
            return type("S", (), {"checkout_url": "https://checkout.example/x"})()

    fake_client = type("C", (), {"customers": _Customers(), "checkout_sessions": _Sessions()})()
    old_client = payments_router.client
    payments_router.client = fake_client
    try:
        with_admin(S, st)
        c.post("/admin/pricing-plans/import-from-dodo")
        pid = next(p["id"] for p in c.get("/admin/pricing-plans").json()["plans"]
                   if (p["plan_key"], p["billing_interval"]) == ("Pro", "monthly"))
        c.put(f"/admin/pricing-plans/{pid}", json={"price_minor": 2500, "credits": 1600})
        d = S(); make_user(d, "buyer@x.com"); d.close(); st["email"] = "buyer@x.com"
        body = {"plan_name": "Pro", "billing_cycle": "monthly", "quantity": 1,
                "price": 1, "amount": 1, "product_id": "pdt_attacker", "credits_to_add": 999999}   # spoof attempts
        r = c.post("/payments/create-checkout-session", json=body)
        assert r.status_code == 200, r.text
        assert captured["product_cart"][0]["product_id"] == "pdt_new_1"           # the NEW active Dodo product
        assert captured["metadata"]["credits_to_add"] == "1600"
        # deactivated plan cannot be bought; unknown plan is rejected
        st["email"] = "admin@x.com"
        c.patch(f"/admin/pricing-plans/{pid}/status", json={"is_active": False})
        st["email"] = "buyer@x.com"
        assert c.post("/payments/create-checkout-session", json=body).status_code == 400
        assert c.post("/payments/create-checkout-session",
                      json={"plan_name": "Nope", "billing_cycle": "monthly"}).status_code == 400
        # untouched plans still use their original product
        captured.clear()
        c.post("/payments/create-checkout-session", json={"plan_name": "Basic", "billing_cycle": "monthly"})
        assert captured["product_cart"][0]["product_id"] == "pdt_0NUQxXnGKlkhrpGBAFMvy"
        assert captured["metadata"]["credits_to_add"] == "600"
    finally:
        payments_router.client = old_client
        done()


def test_23_existing_subscribers_keep_their_product_credits_and_resolve_after_price_change():
    S = new_db(); db = S(); gw = FakeGateway(); import_all(db, gw)
    plan = pricing.get_plan(db, "Pro", "monthly")
    old_pid = pricing.active_price(db, plan.id).gateway_product_id
    plan.credits = 2000                                               # new product will carry 2000 credits
    rid = pricing.begin_price_change(db, plan.id, 3000, "USD", None)
    pricing.sync_price(db, rid, gw)
    new_pid = pricing.active_price(db, plan.id).gateway_product_id
    assert new_pid != old_pid
    # a renewal payment for the OLD product still grants the old credits; new product grants the new ones
    assert pricing.renewal_credits(db, "Pro", "monthly", False, product_id=old_pid) == 1500
    assert pricing.renewal_credits(db, "Pro", "monthly", False, product_id=new_pid) == 2000
    # without a product id the plan's current credits apply
    assert pricing.renewal_credits(db, "Pro", "monthly", False) == 2000
    # retired product still maps to the plan for subscription.updated / sync-subscription
    assert pricing.resolve_by_product_id(db, old_pid, api=False) == ("Pro", "monthly")
    assert pricing.resolve_by_product_id(db, old_pid, api=True) is None       # workspace plan, not API
    # yearly cron credits follow the plan (display-name edits never matter: keyed by plan_key)
    assert pricing.yearly_credits(db, "Pro", api=False) == 18000
    plan.name = "Totally Renamed"; db.commit()
    assert pricing.yearly_credits(db, "Pro", api=False) == 18000


def test_24_admin_creates_a_brand_new_plan_and_validation_rejects_bad_input():
    c, S, st, gw, done = http_env()
    try:
        with_admin(S, st)
        body = {"plan_key": "Ultra", "kind": "workspace", "billing_interval": "monthly", "name": "Ultra",
                "description": "Everything", "credits": 9000, "features": ["9000 credits"], "price_minor": 7900,
                "currency": "usd", "tax_category": "saas", "display_order": 9}
        r = c.post("/admin/pricing-plans", json=body)
        assert r.status_code == 201, r.text
        j = r.json()
        assert j["sync_status"] == "SYNCED" and j["active_price"]["price_minor"] == 7900 and j["currency"] == "USD"
        assert gw.specs[-1]["template_product_id"] is None and gw.specs[-1]["tax_category"] == "saas"
        assert c.post("/admin/pricing-plans", json=body).status_code == 409           # duplicate key+interval
        assert any(p["plan_key"] == "Ultra" for p in c.get("/pricing-plans").json()["plans"])
        pid = j["id"]
        for bad in ({"price_minor": 0}, {"price_minor": -5}, {"price_minor": 1.5}, {"credits": -1},
                    {"currency": "DOLLARS"}, {"tax_category": "weird"}, {"features": ["x"] * 31}):
            assert c.put(f"/admin/pricing-plans/{pid}", json=bad).status_code == 422, bad
        assert c.post("/admin/pricing-plans", json={**body, "plan_key": "X", "kind": "one_time"}).status_code == 422
        assert c.put("/admin/pricing-plans/99999", json={"name": "x"}).status_code == 404
    finally:
        done()


def test_25_pricing_edits_never_modify_users_or_subscriptions():
    c, S, st, gw, done = http_env()
    try:
        with_admin(S, st)
        d = S()
        u = make_user(d, "sub@x.com", plan="Pro", status="active")
        u.billing_cycle, u.subscription_id, u.credits = "monthly", "sub_123", 777
        d.commit()
        before = (u.plan, u.billing_cycle, u.subscription_id, u.subscription_status, u.credits)
        c.post("/admin/pricing-plans/import-from-dodo")
        pid = next(p["id"] for p in c.get("/admin/pricing-plans").json()["plans"]
                   if (p["plan_key"], p["billing_interval"]) == ("Pro", "monthly"))
        c.put(f"/admin/pricing-plans/{pid}", json={"name": "Renamed", "price_minor": 4000, "credits": 5000})
        d2 = S(); u2 = d2.query(User).filter(User.email == "sub@x.com").first()
        assert (u2.plan, u2.billing_cycle, u2.subscription_id, u2.subscription_status, u2.credits) == before
        d.close(); d2.close()
    finally:
        done()


def test_26_dodo_gateway_adapter_clones_template_dedupes_by_metadata_and_verifies():
    """Exercises the real DodoGateway adapter against a stub SDK client built from the real SDK models."""
    from types import SimpleNamespace as NS
    from dodopayments.types.price import RecurringPrice
    from pricing_gateway import DodoGateway

    template_price = RecurringPrice(
        currency="USD", discount=0, price=2000, purchasing_power_parity=False, type="recurring_price",
        payment_frequency_count=1, payment_frequency_interval="Month",
        subscription_period_count=10, subscription_period_interval="Year", tax_inclusive=False)

    class Products:
        def __init__(self):
            self.created, self.store = [], {"pdt_tmpl": NS(price=template_price, tax_category="saas",
                                                          product_id="pdt_tmpl", metadata={})}
        def retrieve(self, pid):
            if pid not in self.store:
                raise RuntimeError("404")
            return self.store[pid]
        def list(self, **kw):
            return list(self.store.values())
        def create(self, **kw):
            self.created.append(kw)
            pid = f"pdt_c{len(self.created)}"
            price = RecurringPrice(**{**kw["price"]})
            self.store[pid] = NS(price=price, tax_category=kw["tax_category"], product_id=pid,
                                 metadata=kw["metadata"])
            return NS(product_id=pid)

    prods = Products()
    gw = DodoGateway(NS(products=prods))
    spec = {"name": "Pro", "description": "d", "price_minor": 2500, "currency": "usd", "interval": "monthly",
            "template_product_id": "pdt_tmpl", "tax_category": None, "metadata": {"plan_key": "Pro", "version": 2}}
    pid = gw.ensure_product("Pro|monthly|v2|2500|USD", spec)
    sent = prods.created[0]
    assert sent["price"]["price"] == 2500 and sent["price"]["currency"] == "USD"
    assert sent["price"]["payment_frequency_interval"] == "Month" and sent["tax_category"] == "saas"
    assert sent["metadata"]["sync_key"] == "Pro|monthly|v2|2500|USD" and sent["metadata"]["version"] == "2"
    # retry with the same key reuses the product instead of creating another
    assert gw.ensure_product("Pro|monthly|v2|2500|USD", spec) == pid and len(prods.created) == 1
    # a product whose price does not match is rejected (never published)
    prods.store["pdt_wrong"] = NS(price=template_price, tax_category="saas", product_id="pdt_wrong",
                                  metadata={"sync_key": "K"})
    try:
        gw.ensure_product("K", spec); raise AssertionError("price mismatch accepted")
    except GatewayError as e:
        assert "expected 2500" in str(e)
    # unreadable template -> clear error, nothing created
    bad = dict(spec, template_product_id="pdt_missing")
    try:
        gw.ensure_product("other", bad); raise AssertionError("expected GatewayError")
    except GatewayError:
        pass
    assert len(prods.created) == 1
    assert gw.read_product("pdt_tmpl") == {"price_minor": 2000, "currency": "USD"}


def test_27_saved_templates_and_background_image():
    c, S, st, gw, done = http_env()
    try:
        d = S(); make_user(d, "user@x.com"); d.close()
        st["email"] = "user@x.com"
        assert c.get("/admin/promo-templates").status_code == 403
        assert c.post("/admin/promo-templates", json={"kind": "popup", "name": "x", "data": {}}).status_code == 403
        with_admin(S, st)
        # background image goes through the same https / in-app-path validation
        r = c.post("/admin/popups", json=popup_body(bg_image_url="https://cdn.example.com/bg.jpg"))
        assert r.status_code == 201 and r.json()["bg_image_url"] == "https://cdn.example.com/bg.jpg", r.text
        assert c.post("/admin/popups", json=popup_body(bg_image_url="javascript:alert(1)")).status_code == 422
        assert c.post("/admin/offer-banner", json={"title": "x", "bg_image_url": "https://cdn.example.com/b.jpg"}).status_code == 201

        data = {"title": "<b>Diwali</b> Sale", "theme": "sparkles", "emoji": "🪔", "bg_color": "#7C2D12",
                "bg_color2": "#581c87", "show_countdown": True, "bg_image_url": "https://cdn.example.com/d.jpg",
                "layout": "stack", "evil_extra_field": "dropped", "start_at": "2020-01-01T00:00:00Z"}
        r = c.post("/admin/promo-templates", json={"kind": "popup", "name": " My <i>Diwali</i> ", "emoji": "🪔",
                                                    "data": data, "end_in_days": 5})
        assert r.status_code == 201, r.text
        t = r.json()
        assert t["name"] == "My Diwali" and t["end_in_days"] == 5
        assert t["data"]["title"] == "Diwali Sale" and t["data"]["bg_color"] == "#7c2d12"          # sanitised + normalised
        assert "evil_extra_field" not in t["data"] and "start_at" not in t["data"]                 # whitelist only
        assert t["data"]["show_countdown"] is True and t["data"]["bg_image_url"].endswith("d.jpg")
        # invalid content is rejected exactly like a real popup would be
        for bad in ({"bg_color": "red"}, {"button_link": "javascript:1"}, {"theme": "fire"}, {"bg_image_url": "data:x"}):
            assert c.post("/admin/promo-templates", json={"kind": "popup", "name": "bad", "data": {"title": "t", **bad}}).status_code == 422, bad
        assert c.post("/admin/promo-templates", json={"kind": "other", "name": "x", "data": {}}).status_code == 422
        assert c.post("/admin/promo-templates", json={"kind": "popup", "name": "  ", "data": {}}).status_code == 422
        # banner template keeps banner-only fields
        rb = c.post("/admin/promo-templates", json={"kind": "banner", "name": "BF", "data": {"title": "BF", "badge_text": "50% OFF", "layout": "stack"}})
        assert rb.status_code == 201 and rb.json()["data"].get("badge_text") == "50% OFF" and "layout" not in rb.json()["data"]
        assert [x["name"] for x in c.get("/admin/promo-templates?kind=popup").json()["templates"]] == ["My Diwali"]
        assert len(c.get("/admin/promo-templates").json()["templates"]) == 2
        assert c.delete(f"/admin/promo-templates/{t['id']}").status_code == 200
        assert c.delete(f"/admin/promo-templates/{t['id']}").status_code == 404
        assert len(c.get("/admin/promo-templates").json()["templates"]) == 1
    finally:
        done()


def test_28_image_only_popup_and_banner():
    c, S, st, gw, done = http_env()
    try:
        with_admin(S, st)
        IMG = "https://cdn.example.com/full.jpg"
        # popup: a finished creative needs only an image (+ optional link, no button text)
        r = c.post("/admin/popups", json={"title": "Eid creative", "display_mode": "image", "image_url": IMG,
                                          "button_link": "/pricing", "status": "ACTIVE"})
        assert r.status_code == 201, r.text
        j = r.json()
        assert j["display_mode"] == "image" and j["image_url"] == IMG and j["button_link"] == "/pricing", j
        assert c.post("/admin/popups", json={"title": "x", "display_mode": "image"}).status_code == 422     # image required
        assert c.post("/admin/popups", json={"title": "x", "display_mode": "poster", "image_url": IMG}).status_code == 422
        # designed mode still needs button text for a link
        assert c.post("/admin/popups", json={"title": "x", "button_link": "/pricing"}).status_code == 422
        # a stale countdown flag is ignored in image mode (no end date needed)
        r = c.post("/admin/popups", json={"title": "x", "display_mode": "image", "image_url": IMG, "show_countdown": True})
        assert r.status_code == 201 and r.json()["show_countdown"] is False, r.text
        assert c.post("/admin/popups", json={"title": "x", "display_mode": "image", "image_url": "javascript:1"}).status_code == 422
        # default is designed
        assert c.post("/admin/popups", json=popup_body()).json()["display_mode"] == "designed"

        # banner: desktop + optional phone image, served by the public endpoint
        rb = c.post("/admin/offer-banner", json={"title": "Sale creative", "display_mode": "image", "image_url": IMG,
                                                 "mobile_image_url": "https://cdn.example.com/m.jpg",
                                                 "button_link": "/pricing", "is_active": True})
        assert rb.status_code == 201, rb.text
        st["email"] = None
        pub = c.get("/offer-banner/active").json()["banner"]
        assert pub["display_mode"] == "image" and pub["mobile_image_url"].endswith("m.jpg") and pub["image_url"] == IMG
        st["email"] = "admin@x.com"
        assert c.post("/admin/offer-banner", json={"title": "x", "display_mode": "image"}).status_code == 422
        assert c.post("/admin/offer-banner", json={"title": "x", "display_mode": "image", "image_url": IMG,
                                                   "mobile_image_url": "data:image/png;base64,AAAA"}).status_code == 422
        # templates can carry image mode too
        rt = c.post("/admin/promo-templates", json={"kind": "banner", "name": "Creative", "data": {
            "title": "t", "display_mode": "image", "image_url": IMG, "mobile_image_url": "https://cdn.example.com/m.jpg"}})
        assert rt.status_code == 201 and rt.json()["data"]["display_mode"] == "image", rt.text
    finally:
        done()


def _wn_body(**kw):
    b = {"tab_label": "Spring update", "badge_text": "New", "title": "Spring is here", "subtitle": "Lots of new things.",
         "features": [{"icon": "rocket", "title": "Faster", "description": "Much faster."}],
         "button1_text": "Go", "button1_link": "/pricing", "button1_style": "dark",
         "button2_text": "Later", "auto_show": False, "status": "ACTIVE"}
    b.update(kw)
    return b


def test_29_whats_new_releases_admin_and_public():
    c, S, st, gw, done = http_env()
    try:
        d = S(); make_user(d, "user@x.com"); d.close()
        st["email"] = "user@x.com"
        assert c.get("/admin/whats-new").status_code == 403
        assert c.post("/admin/whats-new", json=_wn_body()).status_code == 403
        assert c.post("/admin/whats-new/import-defaults").status_code == 403
        st["email"] = None                                              # the public endpoint needs no login
        assert c.get("/whats-new").json() == {"releases": [], "announce_version": None}

        with_admin(S, st)
        # one-time import of the built-in content; never duplicates
        r = c.post("/admin/whats-new/import-defaults")
        assert r.status_code == 201 and r.json()["imported"] == 2, r.text
        assert c.post("/admin/whats-new/import-defaults").status_code == 409
        pub = c.get("/whats-new").json()
        assert [x["tab_label"] for x in pub["releases"]] == ["Latest: App Launch", "Version 2.0 Updates"]
        assert pub["announce_version"] is None                          # imported content is not auto-announced
        assert pub["releases"][0]["button1_style"] == "google_play" and len(pub["releases"][1]["features"]) == 7
        assert "status" not in pub["releases"][0] and "auto_show" not in pub["releases"][0]

        # create: sanitised, normalised, newest first on ties
        r = c.post("/admin/whats-new", json=_wn_body(title="<b>Spring</b> is here", auto_show=True,
                                                     features=[{"icon": "rocket", "title": "<i>Fast</i>", "description": "x"}]))
        assert r.status_code == 201, r.text
        j = r.json()
        assert j["title"] == "Spring is here" and j["features"][0]["title"] == "Fast" and j["auto_show"] is True
        pub = c.get("/whats-new").json()
        assert pub["releases"][0]["id"] == j["id"]                      # display_order 0 ties -> newest first
        assert pub["announce_version"] == j["id"]
        # editing does NOT re-announce; archiving removes it from the modal and the announcement
        assert c.put(f"/admin/whats-new/{j['id']}", json=_wn_body(title="Spring edited", auto_show=True)).status_code == 200
        assert c.get("/whats-new").json()["announce_version"] == j["id"]
        assert c.patch(f"/admin/whats-new/{j['id']}/status", json={"status": "INACTIVE"}).status_code == 200
        pub = c.get("/whats-new").json()
        assert len(pub["releases"]) == 2 and pub["announce_version"] is None
        assert c.delete(f"/admin/whats-new/{j['id']}").json()["status"] == "archived"
        assert c.put(f"/admin/whats-new/{j['id']}", json=_wn_body()).status_code == 409
        assert len(c.get("/admin/whats-new").json()["releases"]) == 2
        assert len(c.get("/admin/whats-new?include_archived=true").json()["releases"]) == 3

        # reorder
        ids = [x["id"] for x in c.get("/admin/whats-new").json()["releases"]]
        assert c.post("/admin/whats-new/reorder", json={"items": [{"id": ids[0], "display_order": 5}, {"id": ids[1], "display_order": 1}]}).status_code == 200
        assert [x["id"] for x in c.get("/whats-new").json()["releases"]] == [ids[1], ids[0]]
        assert c.post("/admin/whats-new/reorder", json={"items": [{"id": 9999, "display_order": 1}]}).status_code == 404

        # validation
        bad = [{"features": [{"icon": "explosion", "title": "x"}]}, {"features": [{"icon": "zap", "title": "  "}]},
               {"features": [{"icon": "zap", "title": "x"}] * 13}, {"button1_link": "javascript:alert(1)"},
               {"button2_link": "/x", "button2_text": None}, {"button1_style": "neon"},
               {"button1_style": "google_play", "button1_link": None}, {"tab_label": "  "}, {"title": ""},
               {"tab_label": "x" * 31}, {"status": "ARCHIVED"}]
        for patch in bad:
            assert c.post("/admin/whats-new", json=_wn_body(**patch)).status_code == 422, patch
        ok = c.post("/admin/whats-new", json=_wn_body(button1_link="https://play.google.com/store/apps/details?id=x", button1_style="google_play"))
        assert ok.status_code == 201, ok.text
    finally:
        done()


# ----------------------------------------------------------------------------------------------------------
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
        for e in _engines:
            e.dispose()
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_())
