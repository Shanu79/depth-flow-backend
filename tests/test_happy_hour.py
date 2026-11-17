"""
Happy Hour v2 tests.   Run from anywhere:   python tests/test_happy_hour.py

Safety: uses throw-away SQLite files in a temp dir and an in-memory DATABASE_URL for the app import.
It never touches the configured database. No pytest needed (plain asserts + a tiny runner).
"""
import os, sys, shutil, tempfile, threading, itertools, traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)                      # the generation endpoints write into ./static
sys.path.insert(0, ROOT)
os.environ["DATABASE_URL"] = "sqlite://"   # must be set before importing the app (dotenv won't override it)

from datetime import datetime, timedelta
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from database import Base
from models import User, HappyHourSession, HappyHourEntitlement, AppSetting
import happy_hour as hh

TMP = tempfile.mkdtemp(prefix="hh_tests_")
_counter = itertools.count()
_engines = []


def new_db(immediate=False):
    """Fresh SQLite file DB. immediate=True -> every transaction starts with BEGIN IMMEDIATE, which makes
    SQLite serialise concurrent writers the way a row lock (SELECT ... FOR UPDATE) does on Postgres."""
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


def make_user(db, email, plan="Pro", cycle="monthly", status="active", credits=100, sub_id="sub_1"):
    u = User(email=email, plan=plan, billing_cycle=cycle, subscription_status=status,
             subscription_id=sub_id, credits=credits, is_verified=True)
    db.add(u); db.commit(); db.refresh(u)
    return u


def grant(db, u, cycle, ref, now, expiry=None, sub_id="sub_1"):
    ent = hh.grant_entitlement(db, u, cycle, sub_id, ref, start=now, expiry=expiry, source="dodo", now=now)
    db.commit()
    return ent


def expect(status_code, code, fn):
    try:
        fn()
    except hh.HappyHourError as e:
        assert e.status_code == status_code and e.code == code, \
            f"expected {status_code}/{code}, got {e.status_code}/{e.code}: {e.detail}"
        return
    raise AssertionError(f"expected HappyHourError {status_code}/{code}, nothing raised")


def simulate_generation(db, u, now, cost=20, fail=False):
    """Mirrors the protocol in the two generation endpoints. Returns True if it ran under Happy Hour."""
    g = hh.begin_generation(db, u, now=now)
    unlimited = g is not None
    if not unlimited and u.credits < cost:
        raise AssertionError("402 not enough credits")
    if fail:
        if g:
            hh.release_generation(db, g)
        return unlimited
    if unlimited:
        hh.confirm_generation(db, g, now=now)
    else:
        u.credits -= cost
    db.commit()
    return unlimited


def sess(db):
    return db.query(HappyHourSession).order_by(HappyHourSession.id.desc()).first()


T = datetime(2026, 3, 10, 10, 30, 0)
H = timedelta(hours=1)

# =============================================================================================== scenarios
def test_01_yearly_gets_4_tokens():
    db = new_db()(); u = make_user(db, "y@x.com")
    ent = grant(db, u, "yearly", "pay_1", T)
    assert (ent.total_allocated_tokens, ent.available_tokens, ent.total_claimed_tokens) == (4, 4, 0)
    assert ent.subscription_type == "yearly" and ent.entitlement_status == "ACTIVE"


def test_02_yearly_one_activation_per_calendar_month():
    db = new_db()(); u = make_user(db, "y@x.com", cycle="yearly"); grant(db, u, "yearly", "pay_1", T)
    s, _ = hh.activate_token(db, u, "a", now=T)
    assert hh.get_entitlement(db, u.id).available_tokens == 3
    expect(409, "SESSION_ACTIVE", lambda: hh.activate_token(db, u, "b", now=T + timedelta(minutes=5)))
    # session over, tokens remain (3) but same calendar month -> blocked
    expect(403, "MONTHLY_LIMIT", lambda: hh.activate_token(db, u, "c", now=T + 2 * H))
    expect(403, "MONTHLY_LIMIT", lambda: hh.activate_token(db, u, "c2", now=datetime(2026, 3, 31, 23, 59)))
    assert hh.get_entitlement(db, u.id).available_tokens == 3          # refused attempts burn nothing
    st = hh.build_status(db, u, now=T + 2 * H)
    assert st["blocked_reason"] == "MONTHLY_LIMIT" and st["next_activation_available_at"] == "2026-04-01T00:00:00Z"
    # next calendar month works
    hh.activate_token(db, u, "d", now=datetime(2026, 4, 1, 0, 0, 1))
    assert hh.get_entitlement(db, u.id).available_tokens == 2


def test_03_yearly_unused_tokens_carry_forward_and_never_exceed_allocation():
    db = new_db()(); u = make_user(db, "y@x.com", cycle="yearly"); grant(db, u, "yearly", "pay_1", datetime(2026, 1, 1))
    ent = hh.get_entitlement(db, u.id)
    plan = [(datetime(2026, 1, 12), 3), (None, 3), (datetime(2026, 3, 3), 2), (datetime(2026, 4, 9), 1), (datetime(2026, 5, 1), 0)]
    for when, left in plan:                      # the example from the spec (Feb skipped)
        if when:
            hh.activate_token(db, u, f"k{when}", now=when)
        db.refresh(ent)
        assert ent.available_tokens == left, (when, ent.available_tokens)
        assert ent.available_tokens + ent.total_claimed_tokens <= ent.total_allocated_tokens
    expect(403, "NO_TOKENS", lambda: hh.activate_token(db, u, "z", now=datetime(2026, 6, 2)))


def test_04_monthly_gets_1_token_per_period():
    db = new_db()(); u = make_user(db, "m@x.com"); ent = grant(db, u, "monthly", "pay_1", T)
    assert (ent.total_allocated_tokens, ent.available_tokens) == (1, 1) and ent.subscription_type == "monthly"
    hh.activate_token(db, u, "a", now=T)
    expect(409, "SESSION_ACTIVE", lambda: hh.activate_token(db, u, "b", now=T + timedelta(minutes=1)))
    expect(403, "NO_TOKENS", lambda: hh.activate_token(db, u, "c", now=T + 2 * H))


def test_05_monthly_token_has_no_rollover_and_renewal_regrants():
    db = new_db()(); u = make_user(db, "m@x.com"); grant(db, u, "monthly", "pay_1", T)
    # unused for the whole period; renewal payment arrives next month
    ent = grant(db, u, "monthly", "pay_2", T + timedelta(days=31))
    assert ent.available_tokens == 1 and ent.total_allocated_tokens == 1        # NOT 2
    # a used token + renewal -> fresh token
    hh.activate_token(db, u, "a", now=T + timedelta(days=32))
    assert hh.get_entitlement(db, u.id).available_tokens == 0
    ent = grant(db, u, "monthly", "pay_3", T + timedelta(days=62))
    assert ent.available_tokens == 1


def test_06_no_activation_without_active_subscription():
    S = new_db(); db = S()
    free = make_user(db, "f@x.com", plan="Free", cycle="monthly")                  # new signups look like this
    expect(403, "NOT_SUBSCRIBED", lambda: hh.activate_token(db, free, "a", now=T))
    pack = make_user(db, "p@x.com", plan="Credit Pack")
    expect(403, "NOT_SUBSCRIBED", lambda: hh.activate_token(db, pack, "a", now=T))
    cancelled = make_user(db, "c@x.com"); grant(db, cancelled, "monthly", "pay_1", T)
    cancelled.subscription_status = "cancelled"; db.commit()
    expect(403, "NOT_SUBSCRIBED", lambda: hh.activate_token(db, cancelled, "a", now=T))
    api_only = make_user(db, "api@x.com", plan="Free", cycle=None)                 # API plans are not eligible
    api_only.api_plan, api_only.api_billing_cycle, api_only.api_subscription_status = "Pro API", "yearly", "active"
    db.commit()
    expect(403, "NOT_SUBSCRIBED", lambda: hh.activate_token(db, api_only, "a", now=T))
    assert db.query(HappyHourSession).count() == 0
    # "Scheduled for cancellation" is still a paid, active subscription until the period ends
    sched = make_user(db, "s@x.com", status="Scheduled for cancellation on 2026-04-01"); grant(db, sched, "monthly", "p", T)
    hh.activate_token(db, sched, "a", now=T)


def test_07_duplicate_activation_requests_cannot_deduct_twice():
    db = new_db()(); u = make_user(db, "y@x.com", cycle="yearly"); grant(db, u, "yearly", "pay_1", T)
    s1, r1 = hh.activate_token(db, u, "same-key", now=T)
    s2, r2 = hh.activate_token(db, u, "same-key", now=T + timedelta(seconds=1))
    s3, r3 = hh.activate_token(db, u, "same-key", now=T + 3 * H)       # replay long after: still the same session
    assert (r1, r2, r3) == (False, True, True) and s1.id == s2.id == s3.id
    ent = hh.get_entitlement(db, u.id)
    assert ent.available_tokens == 3 and ent.total_claimed_tokens == 1
    assert db.query(HappyHourSession).count() == 1


def test_08_multiple_concurrent_sessions_impossible():
    S = new_db(immediate=True); db = S()
    u = make_user(db, "y@x.com", cycle="yearly"); grant(db, u, "yearly", "pay_1", T); uid = u.id
    db.commit()   # end the txn opened by reading u.id, otherwise this session blocks the worker threads
    results, lock = [], threading.Lock()

    def worker(i):
        d = S()
        try:
            user = d.get(User, uid)
            try:
                hh.activate_token(d, user, f"key-{i}", now=T)
                out = "ok"
            except hh.HappyHourError as e:
                out = e.code
            with lock: results.append(out)
        finally:
            d.close()

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert results.count("ok") == 1 and results.count("SESSION_ACTIVE") == 7, results
    db.expire_all()
    assert db.query(HappyHourSession).filter_by(status="ACTIVE").count() == 1
    assert hh.get_entitlement(db, uid).available_tokens == 3           # exactly one token spent
    # DB-level backstop: a second ACTIVE row for the same user is physically rejected
    db.add(HappyHourSession(user_id=uid, status="ACTIVE", started_at=T, expires_at=T + H, max_video_generations=5))
    try:
        db.commit(); raise AssertionError("second ACTIVE session was allowed")
    except IntegrityError:
        db.rollback()


def test_09_closing_browser_or_restarting_does_not_pause_session():
    S = new_db(); db = S(); u = make_user(db, "m@x.com"); grant(db, u, "monthly", "pay_1", T)
    hh.activate_token(db, u, "a", now=T); db.close()
    # "restart": brand-new session/connection, no state kept anywhere but the DB
    db2 = S(); u2 = db2.query(User).first()
    info = hh.build_session_info(db2, u2, now=T + timedelta(minutes=10))
    assert info["active"] and info["session"]["remaining_seconds"] == 3000
    assert info["session"]["expires_at"] == "2026-03-10T11:30:00Z"
    # refreshing / re-activating cannot restart or extend it
    expect(409, "SESSION_ACTIVE", lambda: hh.activate_token(db2, u2, "again", now=T + timedelta(minutes=10)))
    assert hh.build_session_info(db2, u2, now=T + timedelta(minutes=20))["session"]["remaining_seconds"] == 2400


def test_10_session_lasts_exactly_3600_seconds():
    db = new_db()(); u = make_user(db, "m@x.com"); grant(db, u, "monthly", "pay_1", T)
    s, _ = hh.activate_token(db, u, "a", now=T)
    assert hh.HAPPY_HOUR_SECONDS == 3600 and (s.expires_at - s.started_at).total_seconds() == 3600
    assert hh.begin_generation(db, u, now=T + timedelta(seconds=3599)) is not None      # still unlimited
    assert hh.begin_generation(db, u, now=T + timedelta(seconds=3600)) is None            # expired at exactly 3600
    db.refresh(s); assert s.status == "EXPIRED"


def test_11_normal_credit_deduction_resumes_after_expiry():
    db = new_db()(); u = make_user(db, "m@x.com", credits=100); grant(db, u, "monthly", "pay_1", T)
    hh.activate_token(db, u, "a", now=T)
    assert simulate_generation(db, u, T + timedelta(minutes=5)) is True and u.credits == 100
    assert simulate_generation(db, u, T + 61 * timedelta(minutes=1)) is False and u.credits == 80   # after 60 min
    assert sess(db).status == "EXPIRED"
    # token was consumed, no refund
    assert hh.get_entitlement(db, u.id).available_tokens == 0


def test_12_credits_not_deducted_during_session():
    db = new_db()(); u = make_user(db, "m@x.com", credits=0); grant(db, u, "monthly", "pay_1", T)
    hh.activate_token(db, u, "a", now=T)
    for i in range(3):
        assert simulate_generation(db, u, T + timedelta(minutes=i + 1)) is True
    assert u.credits == 0 and sess(db).successful_video_generations == 3


def test_13_admin_limit_enforced_snapshot_and_fallback_to_credits():
    db = new_db()(); u = make_user(db, "y@x.com", cycle="yearly", credits=100); grant(db, u, "yearly", "pay_1", T)
    assert hh.get_max_videos(db) == hh.DEFAULT_MAX_VIDEOS == 10                       # default when unset
    hh.set_max_videos(db, 3, admin_user_id=1)
    s, _ = hh.activate_token(db, u, "a", now=T)
    assert s.max_video_generations == 3
    hh.set_max_videos(db, 50)                                                         # later change: snapshot unaffected
    db.refresh(s); assert s.max_video_generations == 3
    for i in range(3):
        assert simulate_generation(db, u, T + timedelta(minutes=i + 1)) is True
    assert u.credits == 100
    db.refresh(s); assert s.status == "LIMIT_REACHED" and s.successful_video_generations == 3
    # 4th request: falls back to the normal credit logic (user can still pay with credits)
    assert simulate_generation(db, u, T + timedelta(minutes=10)) is False and u.credits == 80
    # a new session picks up the new global value
    s2, _ = hh.activate_token(db, u, "b", now=datetime(2026, 4, 2))
    assert s2.max_video_generations == 50
    # validation: positive integers only
    for bad in (0, -1, "5", 5.5, True, None, hh.MAX_VIDEOS_CEILING + 1):
        try:
            hh.set_max_videos(db, bad); raise AssertionError(f"accepted {bad!r}")
        except ValueError:
            pass
    assert hh.get_max_videos(db) == 50


def test_14_concurrent_generations_cannot_exceed_limit():
    S = new_db(immediate=True); db = S()
    u = make_user(db, "m@x.com", credits=0); grant(db, u, "monthly", "pay_1", T); uid = u.id
    hh.set_max_videos(db, 3); hh.activate_token(db, u, "a", now=T)
    db.commit()
    grants, lock = [], threading.Lock()

    def reserve():
        d = S()
        try:
            g = hh.begin_generation(d, d.get(User, uid), now=T + timedelta(minutes=1))
            if g:
                with lock: grants.append(g)
        finally:
            d.close()

    ts = [threading.Thread(target=reserve) for _ in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(grants) == 3, len(grants)                              # 12 racing requests, only 3 slots
    db.expire_all(); s = sess(db)
    assert s.in_flight_generations == 3 and s.successful_video_generations == 0
    db.rollback()   # release the read txn (BEGIN IMMEDIATE) before the next batch of writers starts

    def finish(g):
        d = S()
        try:
            hh.confirm_generation(d, g, now=T + timedelta(minutes=2)); d.commit()
        finally:
            d.close()

    ts = [threading.Thread(target=finish, args=(g,)) for g in grants]
    [t.start() for t in ts]; [t.join() for t in ts]
    db.expire_all(); s = sess(db)
    assert (s.successful_video_generations, s.in_flight_generations, s.status) == (3, 0, "LIMIT_REACHED")


def test_15_failed_generation_does_not_consume_allowance():
    db = new_db()(); u = make_user(db, "m@x.com", credits=0); grant(db, u, "monthly", "pay_1", T)
    hh.set_max_videos(db, 2); s, _ = hh.activate_token(db, u, "a", now=T)
    for _ in range(5):                                                # five failures in a row
        simulate_generation(db, u, T + timedelta(minutes=1), fail=True)
    db.refresh(s)
    assert (s.successful_video_generations, s.in_flight_generations, s.status) == (0, 0, "ACTIVE")
    assert simulate_generation(db, u, T + timedelta(minutes=2)) is True
    assert simulate_generation(db, u, T + timedelta(minutes=3)) is True
    db.refresh(s); assert s.successful_video_generations == 2 and s.status == "LIMIT_REACHED"


def test_16_cancellation_invalidates_unused_tokens_and_session():
    db = new_db()(); u = make_user(db, "y@x.com", cycle="yearly", credits=0); grant(db, u, "yearly", "pay_1", T)
    s, _ = hh.activate_token(db, u, "a", now=T)
    assert hh.invalidate_entitlement(db, u, hh.ENT_CANCELLED, now=T + timedelta(minutes=10)) is True
    db.commit()
    ent = hh.get_entitlement(db, u.id)
    assert ent.available_tokens == 0 and ent.entitlement_status == "CANCELLED"
    db.refresh(s); assert s.status == "INVALIDATED"
    assert hh.begin_generation(db, u, now=T + timedelta(minutes=11)) is None        # no more free videos
    u.subscription_status = "cancelled"; db.commit()
    expect(403, "NOT_SUBSCRIBED", lambda: hh.activate_token(db, u, "b", now=datetime(2026, 4, 5)))
    st = hh.build_status(db, u, now=datetime(2026, 4, 5))
    assert st["eligible"] is False and st["available_tokens"] == 0 and st["active"] is False


def test_17_expired_subscription_blocks_happy_hour_usage():
    db = new_db()(); u = make_user(db, "m@x.com", credits=0)
    expiry = T + timedelta(days=30)
    grant(db, u, "monthly", "pay_1", T, expiry=expiry)
    # sub period ended and no renewal payment arrived
    after = expiry + timedelta(minutes=1)
    expect(403, "NOT_SUBSCRIBED", lambda: hh.activate_token(db, u, "a", now=after))
    assert hh.get_entitlement(db, u.id).entitlement_status == "EXPIRED"
    # a session that is running when the subscription period ends stops granting privileges
    db2 = new_db()(); u2 = make_user(db2, "m2@x.com", credits=0)
    grant(db2, u2, "monthly", "pay_1", T, expiry=T + timedelta(minutes=20))
    s, _ = hh.activate_token(db2, u2, "a", now=T)
    assert hh.begin_generation(db2, u2, now=T + timedelta(minutes=10)) is not None
    assert hh.begin_generation(db2, u2, now=T + timedelta(minutes=21)) is None


def test_18_renewals_and_replays_do_not_duplicate_tokens():
    db = new_db()(); u = make_user(db, "y@x.com", cycle="yearly"); grant(db, u, "yearly", "pay_1", T)
    hh.activate_token(db, u, "a", now=T)
    for _ in range(3):                                                # duplicate webhook deliveries of the SAME payment
        grant(db, u, "yearly", "pay_1", T + timedelta(days=1))
    ent = hh.get_entitlement(db, u.id)
    assert (ent.available_tokens, ent.total_allocated_tokens, ent.total_claimed_tokens) == (3, 4, 1)
    assert db.query(HappyHourEntitlement).count() == 1
    # plan change (new subscription id/payment) REPLACES, never stacks
    ent = grant(db, u, "monthly", "pay_9", T + timedelta(days=2), sub_id="sub_2")
    assert (ent.subscription_type, ent.total_allocated_tokens, ent.available_tokens) == ("monthly", 1, 1)
    # re-buying inside the same calendar month does not bypass the 1-per-month rule
    expect(403, "MONTHLY_LIMIT", lambda: hh.activate_token(db, u, "b", now=T + 3 * H))
    # genuine next-year renewal -> fresh 4
    ent = grant(db, u, "yearly", "pay_10", T + timedelta(days=365), sub_id="sub_2")
    assert (ent.total_allocated_tokens, ent.available_tokens) == (4, 4)


# --------------------------------------------------------------------------------------- extra scenarios
def test_19_month_boundary_session_survives_and_next_month_independent():
    db = new_db()(); u = make_user(db, "y@x.com", cycle="yearly"); grant(db, u, "yearly", "pay_1", datetime(2026, 1, 1))
    jan = datetime(2026, 1, 31, 23, 45)
    s, _ = hh.activate_token(db, u, "j", now=jan)
    assert s.expires_at == datetime(2026, 2, 1, 0, 45)
    assert hh.begin_generation(db, u, now=datetime(2026, 2, 1, 0, 10)) is not None   # not cut by the month change
    expect(409, "SESSION_ACTIVE", lambda: hh.activate_token(db, u, "f0", now=datetime(2026, 2, 1, 0, 10)))
    assert hh.begin_generation(db, u, now=datetime(2026, 2, 1, 0, 45)) is None
    hh.activate_token(db, u, "f1", now=datetime(2026, 2, 1, 1, 0))                    # Feb activation is independent
    assert hh.get_entitlement(db, u.id).available_tokens == 2


def test_20_existing_subscribers_are_bootstrapped_lazily():
    db = new_db()(); u = make_user(db, "old@x.com", cycle="yearly")                   # paid before this feature existed
    assert hh.get_entitlement(db, u.id) is None
    st = hh.build_status(db, u, now=T)
    assert st["eligible"] and st["available_tokens"] == 4 and st["subscription_type"] == "yearly"
    assert hh.get_entitlement(db, u.id).source == "bootstrap"
    again = hh.build_status(db, u, now=T + timedelta(days=3))                           # no second grant
    assert again["available_tokens"] == 4
    free = make_user(db, "free@x.com", plan="Free")
    st = hh.build_status(db, free, now=T)
    assert st["eligible"] is False and st["blocked_reason"] == "NOT_SUBSCRIBED" and hh.get_entitlement(db, free.id) is None


# ============================================================================================ HTTP level
def _http_env():
    """Real FastAPI app (real routers, real error handler) on a temp DB, user chosen per call, fake render engine."""
    import httpx
    from fastapi import Depends
    from fastapi.testclient import TestClient
    import main
    from database import get_db
    from auth import get_current_user
    from routers import depthflow_ai_router

    S = new_db()
    state = {"email": None}

    def _db():
        d = S()
        try:
            yield d
        finally:
            d.close()

    def _user(d=Depends(get_db)):
        return d.query(User).filter(User.email == state["email"]).first()

    main.app.dependency_overrides[get_db] = _db
    main.app.dependency_overrides[get_current_user] = _user

    class FakeResp:
        content = b"fake-mp4"
        def raise_for_status(self): pass

    class FakeClient:
        fail = False
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            if FakeClient.fail:
                raise httpx.ConnectError("engine down")
            return FakeResp()

    real = depthflow_ai_router.httpx.AsyncClient
    depthflow_ai_router.httpx.AsyncClient = FakeClient
    before = set(os.listdir("static")) if os.path.isdir("static") else set()

    def cleanup():
        depthflow_ai_router.httpx.AsyncClient = real
        main.app.dependency_overrides.clear()
        if os.path.isdir("static"):
            for f in set(os.listdir("static")) - before:
                try: os.remove(os.path.join("static", f))
                except OSError: pass

    return TestClient(main.app), S, state, FakeClient, cleanup


def test_21_http_end_to_end():
    c, S, state, FakeClient, cleanup = _http_env()
    try:
        db = S()
        yearly = make_user(db, "y@x.com", cycle="yearly", credits=0); grant(db, yearly, "yearly", "pay_1", hh.utcnow())
        admin = make_user(db, "admin@x.com", plan="Free"); admin.is_admin = True; db.commit()
        free = make_user(db, "f@x.com", plan="Free", credits=0)
        gen = lambda: c.post("/ai/depthflow/generate-3d", files={"file": ("a.png", b"x", "image/png")})

        # --- admin setting
        state["email"] = "admin@x.com"
        assert c.get("/admin/settings/happy-hour").json()["max_videos_per_session"] == 10
        for bad in (0, -5, "7", 2.5, True, 100000):
            assert c.put("/admin/settings/happy-hour", json={"max_videos_per_session": bad}).status_code == 422, bad
        assert c.put("/admin/settings/happy-hour", json={"max_videos_per_session": 2}).json()["max_videos_per_session"] == 2
        state["email"] = "y@x.com"
        assert c.get("/admin/settings/happy-hour").status_code == 403          # non-admin

        # --- status / activate
        st = c.get("/happy-hour/status").json()
        assert st["eligible"] and st["available_tokens"] == 4 and st["can_activate_this_month"] and not st["active"]
        assert gen().status_code == 402                                         # 0 credits, no session
        r = c.post("/happy-hour/activate", headers={"Idempotency-Key": "k1"}); j = r.json()
        assert r.status_code == 200 and j["active"] and j["available_tokens"] == 3
        assert j["session"]["max_video_generations"] == 2 and j["remaining_video_allowance"] == 2
        assert j["idempotent_replay"] is False
        assert (hh_exp := j["session"]["remaining_seconds"]) in (3599, 3600)
        j2 = c.post("/happy-hour/activate", headers={"Idempotency-Key": "k1"}).json()
        assert j2["idempotent_replay"] is True and j2["available_tokens"] == 3
        r = c.post("/happy-hour/activate", headers={"Idempotency-Key": "k2"})
        assert r.status_code == 409 and r.json()["code"] == "SESSION_ACTIVE" and isinstance(r.json()["detail"], str)
        assert c.get("/happy-hour/session").json()["session"]["status"] == "ACTIVE"

        # --- generation under Happy Hour: no credits needed, none deducted; failures free the slot
        r = gen(); assert r.status_code == 200, r.text
        assert r.headers["x-happy-hour"] == "active" and r.headers["x-cost"] == "0" and r.headers["x-remaining-credits"] == "0"
        assert r.headers["x-happy-hour-remaining-videos"] == "1"
        FakeClient.fail = True
        assert gen().status_code == 500
        FakeClient.fail = False
        assert c.get("/happy-hour/status").json()["remaining_video_allowance"] == 1      # failure did not count
        r = gen(); assert r.status_code == 200 and r.headers["x-happy-hour-remaining-videos"] == "0"
        # cap reached -> falls back to normal credit rules (0 credits => 402)
        assert gen().status_code == 402
        st = c.get("/happy-hour/status").json()
        assert st["active"] is False and c.get("/happy-hour/history").json()[0]["status"] == "LIMIT_REACHED"
        assert st["blocked_reason"] == "MONTHLY_LIMIT"
        r = c.post("/happy-hour/activate", headers={"Idempotency-Key": "k3"})
        assert r.status_code == 403 and r.json()["code"] == "MONTHLY_LIMIT"

        # --- not eligible
        state["email"] = "f@x.com"
        r = c.post("/happy-hour/activate"); assert r.status_code == 403 and r.json()["code"] == "NOT_SUBSCRIBED"
        assert c.get("/happy-hour/status").json()["eligible"] is False

        # --- cancellation voids everything (what the Dodo/Play webhooks do)
        state["email"] = "y@x.com"
        db2 = S(); yu = db2.query(User).filter_by(email="y@x.com").one()
        hh.invalidate_entitlement(db2, yu, hh.ENT_CANCELLED); yu.subscription_status = "cancelled"; db2.commit(); db2.close()
        st = c.get("/happy-hour/status").json()
        assert st["eligible"] is False and st["available_tokens"] == 0
    finally:
        cleanup()


def test_22_billing_webhooks_grant_and_invalidate():
    """Real /payments/webhook + /payments/google-play-webhook handlers, signed with a test secret, Dodo stubbed."""
    import base64, json, uuid
    from standardwebhooks.webhooks import Webhook
    from routers import payments_router as pr
    c, S, state, _Fake, cleanup = _http_env()
    secret = "whsec_" + base64.b64encode(b"unit-test-secret-0123456789").decode()
    saved = (pr.WEBHOOK_SECRET, pr._dodo_period_end)
    pr.WEBHOOK_SECRET = secret
    period_end = hh.utcnow() + timedelta(days=365)
    pr._dodo_period_end = lambda sub_id: period_end          # no network
    try:
        db = S(); u = make_user(db, "buyer@x.com", plan="Free", cycle=None, status="inactive", credits=0, sub_id=None)
        uid = u.id; db.close()

        def send(event, data):
            body = json.dumps({"type": event, "data": data})
            mid = f"msg_{uuid.uuid4()}"; ts = datetime.now(__import__("datetime").timezone.utc)
            sig = Webhook(secret).sign(mid, ts, body)
            r = c.post("/payments/webhook", content=body, headers={
                "webhook-id": mid, "webhook-timestamp": str(int(ts.timestamp())),
                "webhook-signature": sig, "content-type": "application/json"})
            assert r.status_code == 200 and r.json()["status"] in ("received", "ignored"), r.text
            return r.json()

        def ent():
            d = S(); d.expire_all()
            e = d.query(HappyHourEntitlement).filter_by(user_id=uid).first()
            out = None if e is None else (e.subscription_type, e.total_allocated_tokens, e.available_tokens,
                                          e.entitlement_status, e.subscription_expiry_date)
            d.close(); return out

        # new yearly purchase -> 4 tokens, expiry from the (stubbed) Dodo period end
        buy = {"payment_id": "pay_a", "subscription_id": "sub_new", "customer": {"email": "buyer@x.com"},
               "metadata": {"user_id": str(uid), "credits_to_add": "18000", "plan_name": "Pro",
                            "billing_cycle": "yearly", "is_one_time": "false", "is_api": "false", "old_sub_id": ""}}
        send("payment.succeeded", buy)
        assert ent() == ("yearly", 4, 4, "ACTIVE", period_end), ent()
        d = S(); assert d.query(User).get(uid).subscription_end_date == period_end; d.close()
        # same payment delivered twice -> still one allocation
        send("payment.succeeded", buy)
        assert ent()[1:3] == (4, 4)

        # user spends a token, then the yearly RENEWAL payment (no metadata) re-grants a fresh 4
        d = S(); hh.activate_token(d, d.query(User).get(uid), "k", now=hh.utcnow()); d.close()
        assert ent()[2] == 3
        renewal = {"payment_id": "pay_b", "subscription_id": "sub_new", "customer": {"email": "buyer@x.com"}, "metadata": {}}
        send("payment.succeeded", renewal)
        assert ent()[1:4] == (4, 4, "ACTIVE"), ent()
        send("payment.succeeded", renewal)                                  # duplicate renewal webhook
        assert ent()[1:3] == (4, 4)

        # cancellation/expiry event -> tokens void, running session invalidated
        send("subscription.cancelled", {"subscription_id": "sub_new"})
        assert ent()[2:4] == (0, "CANCELLED")
        d = S(); assert d.query(HappyHourSession).filter_by(user_id=uid, status="INVALIDATED").count() == 1; d.close()

        # Google Play: RTDN type 13 (expired) voids a Play subscriber's tokens
        d = S(); pu = make_user(d, "play@x.com", cycle="monthly", sub_id=None); pu.play_purchase_token = "tok123"
        hh.grant_entitlement(d, pu, "monthly", "tok123", "GPA.1", now=hh.utcnow()); d.commit(); pid = pu.id; d.close()
        note = base64.b64encode(json.dumps({"subscriptionNotification": {"notificationType": 13, "purchaseToken": "tok123"}}).encode()).decode()
        assert c.post("/payments/google-play-webhook", json={"message": {"data": note}}).json()["status"] == "success"
        d = S(); e = d.query(HappyHourEntitlement).filter_by(user_id=pid).one()
        assert (e.available_tokens, e.entitlement_status) == (0, "CANCELLED"); d.close()
    finally:
        pr.WEBHOOK_SECRET, pr._dodo_period_end = saved
        cleanup()


# ================================================================================================= runner
def main_():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception:
            failed += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
    for e in _engines:
        e.dispose()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main_()
