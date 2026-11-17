"""
Happy Hour notifier tests.   Run:  python tests/test_notifications.py

Reuses the helpers from tests/test_happy_hour.py (temp SQLite DBs, in-memory DATABASE_URL, real FastAPI app).
FCM is stubbed: nothing leaves the machine. Never touches the configured database.
"""
import os, sys, shutil, traceback, json

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["NOTIFY_PUSH_SYNC"] = "true"        # push jobs run inline so assertions are deterministic
os.environ["NOTIFY_SWEEP_ENABLED"] = "false"   # no background loop during tests
os.environ.pop("FCM_DRY_RUN", None)

import test_happy_hour as base                 # sets DATABASE_URL=sqlite://, chdir, sys.path
from test_happy_hour import new_db, make_user, grant, T, H, _http_env

from datetime import datetime, timedelta
import happy_hour as hh
import notifications as nt
import push_fcm
from models import (User, Notification, DeviceToken, HappyHourSession, NotificationBroadcast)

TOK = lambda n: (f"fcm-token-{n}-" + "x" * 30)          # >= 20 chars like a real FCM token

# ------------------------------------------------------------------------------ FCM stub
PUSHES = []
_real_send = push_fcm.send_to_token


def _fake_send(token, title, body, data=None):
    PUSHES.append({"token": token, "title": title, "body": body, "data": dict(data or {})})
    return "sent"


push_fcm.send_to_token = _fake_send


def reset_pushes():
    PUSHES.clear()


def notes(db, uid, type_=None):
    q = db.query(Notification).filter(Notification.user_id == uid)
    if type_:
        q = q.filter(Notification.type == type_)
    return q.order_by(Notification.id).all()


# ================================================================================================ inbox
def test_01_inbox_isolation_read_state_and_paging():
    S = new_db(); db = S()
    a = make_user(db, "a@x.com"); b = make_user(db, "b@x.com")
    ids = []
    for i in range(5):
        n = nt.notify(db, a.id, nt.HAPPY_HOUR_BROADCAST, f"t{i}", f"b{i}", dedupe_key=f"k{i}", now=T + timedelta(minutes=i))
        ids.append(n.id)
    other = nt.notify(db, b.id, nt.HAPPY_HOUR_BROADCAST, "secret", "b", dedupe_key="k0")
    assert nt.unread_count(db, a.id) == 5 and nt.unread_count(db, b.id) == 1
    page = nt.list_notifications(db, a.id, limit=2)
    assert [n.id for n in page] == [ids[4], ids[3]]                                  # newest first
    assert [n.id for n in nt.list_notifications(db, a.id, limit=10, before_id=ids[2])] == [ids[1], ids[0]]
    assert [n.id for n in nt.list_notifications(db, a.id, limit=10, after_id=ids[2])] == [ids[4], ids[3]]
    # cannot touch somebody else's notification
    assert nt.mark_read(db, a.id, other.id) is False and nt.unread_count(db, b.id) == 1
    assert nt.mark_read(db, a.id, ids[0]) is True and nt.unread_count(db, a.id) == 4
    assert nt.mark_read(db, a.id, ids[0]) is True                                    # idempotent
    assert [n.id for n in nt.list_notifications(db, a.id, unread_only=True, limit=10)] == [ids[4], ids[3], ids[2], ids[1]]
    assert nt.mark_all_read(db, a.id) == 4 and nt.unread_count(db, a.id) == 0
    assert nt.unread_count(db, b.id) == 1                                            # other user untouched
    s = nt.serialize_notification(page[0])
    assert s["data"]["route"] == "happy_hour" and s["created_at"].endswith("Z") and s["read_at"].endswith("Z")   # it was read by mark_all_read above


def test_02_dedupe_key_makes_notify_idempotent():
    S = new_db(); db = S(); reset_pushes()
    u = make_user(db, "a@x.com"); nt.register_device(db, u.id, TOK(1))
    n1 = nt.notify(db, u.id, nt.HAPPY_HOUR_STARTED, "t", "b", dedupe_key="same")
    n2 = nt.notify(db, u.id, nt.HAPPY_HOUR_STARTED, "t", "b", dedupe_key="same")
    assert n1 is not None and n2 is None
    assert len(notes(db, u.id)) == 1 and len(PUSHES) == 1                           # one row, one push


# =============================================================================================== devices
def test_03_device_register_move_and_unregister():
    S = new_db(); db = S()
    a = make_user(db, "a@x.com"); b = make_user(db, "b@x.com")
    nt.register_device(db, a.id, TOK(1), "android", "1.0")
    nt.register_device(db, a.id, TOK(1), "android", "1.1")                          # upsert, no duplicate
    assert db.query(DeviceToken).count() == 1 and db.query(DeviceToken).one().app_version == "1.1"
    nt.register_device(db, b.id, TOK(1), "android")                                 # shared phone: token moves to b
    assert db.query(DeviceToken).one().user_id == b.id
    assert nt.unregister_device(db, a.id, TOK(1)) is False                          # a no longer owns it
    assert db.query(DeviceToken).count() == 1
    assert nt.unregister_device(db, b.id, TOK(1)) is True and db.query(DeviceToken).count() == 0
    # a disabled (dead) token is re-enabled when the app registers it again
    d = nt.register_device(db, a.id, TOK(2)); d.disabled_at = datetime.utcnow(); db.commit()
    nt.register_device(db, a.id, TOK(2)); assert db.query(DeviceToken).one().disabled_at is None


# ============================================================================================= triggers
def test_04_token_granted_notifies_once_per_payment():
    from routers import payments_router as pr
    S = new_db(); db = S(); reset_pushes()
    u = make_user(db, "y@x.com", cycle="yearly"); nt.register_device(db, u.id, TOK(1))
    kw = dict(subscription_id="sub_1", cycle="yearly", expiry=None, source="dodo")
    pr._grant_happy_hour(db, u, grant_ref="pay_1", **kw)
    ns = notes(db, u.id, nt.HAPPY_HOUR_TOKEN_AVAILABLE)
    assert len(ns) == 1 and "4 Happy Hour tokens" in ns[0].body and ns[0].title == "Your Happy Hour tokens are ready"
    assert len(PUSHES) == 1 and PUSHES[0]["data"]["type"] == nt.HAPPY_HOUR_TOKEN_AVAILABLE
    assert PUSHES[0]["data"]["route"] == "happy_hour" and PUSHES[0]["data"]["notification_id"] == ns[0].id
    for _ in range(3):                                                              # duplicate webhook deliveries
        pr._grant_happy_hour(db, u, grant_ref="pay_1", **kw)
    assert len(notes(db, u.id)) == 1 and len(PUSHES) == 1
    # next renewal = new payment = new notice; monthly wording
    m = make_user(db, "m@x.com"); pr._grant_happy_hour(db, m, subscription_id="s", grant_ref="p9", cycle="monthly",
                                                         expiry=None, source="dodo")
    assert notes(db, m.id)[0].title == "Your Happy Hour token is ready"
    pr._grant_happy_hour(db, u, grant_ref="pay_2", **kw)
    assert len(notes(db, u.id)) == 2
    # a push failure never undoes the grant
    push_fcm.send_to_token = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fcm down"))
    try:
        pr._grant_happy_hour(db, u, grant_ref="pay_3", **kw)
    finally:
        push_fcm.send_to_token = _fake_send
    assert hh.get_entitlement(db, u.id).last_grant_ref == "pay_3" and len(notes(db, u.id)) == 3


def test_05_session_lifecycle_notices_via_sweep():
    S = new_db(); db = S(); reset_pushes()
    u = make_user(db, "m@x.com"); grant(db, u, "monthly", "p1", T); nt.register_device(db, u.id, TOK(1))
    s, _ = hh.activate_token(db, u, "k", now=T)
    nt.notify_session_started(db, s)
    started = notes(db, u.id, nt.HAPPY_HOUR_STARTED)
    assert len(started) == 1 and "60 minutes" in started[0].body
    nt.notify_session_started(db, s); assert len(notes(db, u.id, nt.HAPPY_HOUR_STARTED)) == 1
    assert nt.run_notification_sweep(db, now=T + timedelta(minutes=30)) == {"expired": 0, "ended": 0, "ending_soon": 0, "month_unlock": 0}
    assert nt.run_notification_sweep(db, now=T + timedelta(minutes=49))["ending_soon"] == 0
    c = nt.run_notification_sweep(db, now=T + timedelta(minutes=51))
    assert c["ending_soon"] == 1 and c["ended"] == 0
    soon = notes(db, u.id, nt.HAPPY_HOUR_ENDING_SOON)[0]
    assert "9 minutes" in soon.body and json.loads(soon.data)["session_id"] == s.id
    assert nt.run_notification_sweep(db, now=T + timedelta(minutes=55))["ending_soon"] == 0     # already sent
    c = nt.run_notification_sweep(db, now=T + timedelta(minutes=61))
    assert c["expired"] == 1 and c["ended"] == 1 and c["ending_soon"] == 0
    ended = notes(db, u.id, nt.HAPPY_HOUR_ENDED)[0]
    assert ended.title == "Happy Hour has ended" and "credit" in ended.body
    db.refresh(s); assert s.status == "EXPIRED"
    assert nt.run_notification_sweep(db, now=T + timedelta(minutes=62)) == {"expired": 0, "ended": 0, "ending_soon": 0, "month_unlock": 0}
    assert [p["data"]["type"] for p in PUSHES] == ["HAPPY_HOUR_STARTED", "HAPPY_HOUR_ENDING_SOON", "HAPPY_HOUR_ENDED"]
    # a session that expired days ago (sweep was down) gets no stale "ended" push
    reset_pushes(); u2 = make_user(db, "m2@x.com"); grant(db, u2, "monthly", "p2", T); nt.register_device(db, u2.id, TOK(2))
    hh.activate_token(db, u2, "k2", now=T)
    nt.run_notification_sweep(db, now=T + timedelta(days=3))
    assert notes(db, u2.id, nt.HAPPY_HOUR_ENDED) == []


def test_06_ended_notice_for_video_cap_and_cancellation():
    S = new_db(); db = S()
    hh.set_max_videos(db, 1)
    cap = make_user(db, "cap@x.com", credits=0); grant(db, cap, "monthly", "p1", T)
    hh.activate_token(db, cap, "k", now=T)
    g = hh.begin_generation(db, cap, now=T + timedelta(minutes=5))
    hh.confirm_generation(db, g, now=T + timedelta(minutes=6)); db.commit()
    can = make_user(db, "can@x.com"); grant(db, can, "monthly", "p2", T, sub_id="s2")
    hh.activate_token(db, can, "k", now=T)
    hh.invalidate_entitlement(db, can, hh.ENT_CANCELLED, now=T + timedelta(minutes=7)); db.commit()
    nt.run_notification_sweep(db, now=T + timedelta(minutes=8))
    a = notes(db, cap.id, nt.HAPPY_HOUR_ENDED)[0]; b = notes(db, can.id, nt.HAPPY_HOUR_ENDED)[0]
    assert a.title == "Happy Hour video limit reached" and "1 free videos" in a.body
    assert "subscription" in b.body and json.loads(b.data)["status"] == "INVALIDATED"


def test_07_yearly_month_unlock_notice():
    S = new_db(); db = S(); reset_pushes()
    jan = datetime(2026, 1, 1); mar5 = datetime(2026, 3, 5, 12, 0)
    due = make_user(db, "due@x.com", cycle="yearly"); grant(db, due, "yearly", "r1", jan)
    used = make_user(db, "used@x.com", cycle="yearly"); grant(db, used, "yearly", "r2", jan, sub_id="s2")
    hh.activate_token(db, used, "k", now=datetime(2026, 3, 2))                       # already used March
    fresh = make_user(db, "fresh@x.com", cycle="yearly"); grant(db, fresh, "yearly", "r3", datetime(2026, 3, 1), sub_id="s3")
    stale = make_user(db, "stale@x.com", cycle="yearly"); grant(db, stale, "yearly", "r4", jan, expiry=datetime(2026, 3, 1), sub_id="s4")
    gone = make_user(db, "gone@x.com", cycle="yearly", status="cancelled"); grant(db, gone, "yearly", "r5", jan, sub_id="s5")
    monthly = make_user(db, "mo@x.com"); grant(db, monthly, "monthly", "r6", jan, sub_id="s6")
    c = nt.run_notification_sweep(db, now=mar5)
    assert c["month_unlock"] == 1, c
    n = notes(db, due.id, nt.HAPPY_HOUR_TOKEN_AVAILABLE)
    assert len(n) == 1 and n[0].title == "Happy Hour is available this month" and "4 tokens" in n[0].body
    for u in (used, fresh, stale, gone, monthly):
        assert notes(db, u.id, nt.HAPPY_HOUR_TOKEN_AVAILABLE) == [], u.email
    assert nt.run_notification_sweep(db, now=mar5 + timedelta(hours=3))["month_unlock"] == 0   # once per month
    assert nt.run_notification_sweep(db, now=datetime(2026, 4, 1, 0, 1))["month_unlock"] >= 1   # April reopens


# ================================================================================================== FCM
def test_08_fcm_payload_dead_tokens_retry_and_disabled():
    class Resp:
        def __init__(self, code, body=None): self.status_code, self._b, self.text = code, body or {}, json.dumps(body or {})
        def json(self): return self._b

    calls = []
    seq = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json})
        return seq.pop(0)

    saved = (push_fcm.requests.post, push_fcm._access_token, push_fcm.time.sleep)
    push_fcm.requests.post, push_fcm._access_token, push_fcm.time.sleep = fake_post, (lambda: "ya29.fake"), (lambda s: None)
    env = {k: os.environ.get(k) for k in ("FIREBASE_PROJECT_ID", "FIREBASE_SERVICE_ACCOUNT_FILE", "FCM_DRY_RUN")}
    try:
        # --- not configured -> disabled, no network
        for k in env: os.environ.pop(k, None)
        assert _real_send(TOK(1), "t", "b", {}) == "disabled" and calls == []
        # --- configured
        os.environ["FIREBASE_PROJECT_ID"] = "demo-proj"; os.environ["FIREBASE_SERVICE_ACCOUNT_FILE"] = "x.json"
        seq[:] = [Resp(200)]
        assert _real_send(TOK(1), "Happy Hour is live!", "Unlimited", {"type": "HAPPY_HOUR_STARTED", "session_id": 7, "x": None}) == "sent"
        c = calls[-1]; m = c["json"]["message"]
        assert c["url"] == "https://fcm.googleapis.com/v1/projects/demo-proj/messages:send"
        assert c["headers"]["Authorization"] == "Bearer ya29.fake"
        assert m["token"] == TOK(1) and m["notification"] == {"title": "Happy Hour is live!", "body": "Unlimited"}
        assert m["data"] == {"type": "HAPPY_HOUR_STARTED", "session_id": "7", "x": ""}          # all strings
        assert m["android"]["priority"] == "HIGH" and m["android"]["notification"]["channel_id"] == "happy_hour"
        # --- transient failure then success -> retried once
        seq[:] = [Resp(503), Resp(200)]; n = len(calls)
        assert _real_send(TOK(1), "t", "b", {}) == "sent" and len(calls) == n + 2
        # --- dead tokens (404 and UNREGISTERED)
        seq[:] = [Resp(404, {"error": {"status": "NOT_FOUND"}})]
        assert _real_send(TOK(1), "t", "b", {}) == "dead_token"
        seq[:] = [Resp(400, {"error": {"details": [{"errorCode": "UNREGISTERED"}]}})]
        assert _real_send(TOK(1), "t", "b", {}) == "dead_token"
        seq[:] = [Resp(400, {"error": {"message": "bad"}})]
        assert _real_send(TOK(1), "t", "b", {}) == "failed"
        # --- dry run never touches the network
        os.environ["FCM_DRY_RUN"] = "true"; n = len(calls)
        assert _real_send(TOK(1), "t", "b", {}) == "dry_run" and len(calls) == n
        os.environ.pop("FCM_DRY_RUN")

        # --- end to end: a dead token gets disabled and is skipped next time
        S = new_db(); db = S(); nt.set_session_factory(S)
        push_fcm.send_to_token = _real_send
        u = make_user(db, "d@x.com"); nt.register_device(db, u.id, TOK(1)); nt.register_device(db, u.id, TOK(2))
        seq[:] = [Resp(404), Resp(200)]
        nt.notify(db, u.id, nt.HAPPY_HOUR_BROADCAST, "t", "b", dedupe_key="e1")
        db.expire_all()
        states = {d.token: d.disabled_at is not None for d in db.query(DeviceToken).all()}
        assert states == {TOK(1): True, TOK(2): False}, states
        seq[:] = [Resp(200)]; n = len(calls)
        nt.notify(db, u.id, nt.HAPPY_HOUR_BROADCAST, "t", "b", dedupe_key="e2")
        assert len(calls) == n + 1 and calls[-1]["json"]["message"]["token"] == TOK(2)
    finally:
        push_fcm.requests.post, push_fcm._access_token, push_fcm.time.sleep = saved
        push_fcm.send_to_token = _fake_send
        for k, v in env.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v


# ================================================================================================= HTTP
def test_09_http_inbox_devices_and_activate_notification():
    c, S, state, _Fake, cleanup = _http_env(); nt.set_session_factory(S); reset_pushes()
    try:
        db = S(); u = make_user(db, "y@x.com", cycle="yearly", credits=0); grant(db, u, "yearly", "p1", hh.utcnow())
        other = make_user(db, "o@x.com"); db.close()
        state["email"] = "y@x.com"
        # devices: validation + register
        assert c.post("/notifications/devices", json={"token": "short"}).status_code == 422
        assert c.post("/notifications/devices", json={"token": TOK(1), "platform": "ios"}).status_code == 422
        assert c.post("/notifications/devices", json={"token": TOK(1), "app_version": "2.3.4"}).json() == {"status": "registered"}
        assert c.get("/notifications/unread-count").json()["unread_count"] == 0
        # activating pushes "live" + puts it in the inbox; a replay does not notify again
        assert c.post("/happy-hour/activate", headers={"Idempotency-Key": "k1"}).status_code == 200
        assert c.post("/happy-hour/activate", headers={"Idempotency-Key": "k1"}).json()["idempotent_replay"] is True
        assert [p["data"]["type"] for p in PUSHES] == ["HAPPY_HOUR_STARTED"]
        r = c.get("/notifications").json()
        assert r["unread_count"] == 1 and len(r["items"]) == 1 and "server_time" in r
        item = r["items"][0]
        assert item["type"] == "HAPPY_HOUR_STARTED" and item["title"] == "Happy Hour is live!" and item["read_at"] is None
        assert item["data"]["route"] == "happy_hour" and item["data"]["expires_at"].endswith("Z")
        assert c.get(f"/notifications?after_id={item['id']}").json()["items"] == []
        assert c.get("/notifications?unread_only=true").json()["unread_count"] == 1
        assert c.get("/notifications?limit=0").status_code == 422
        # someone else's notification: indistinguishable from missing
        state["email"] = "o@x.com"
        assert c.get("/notifications").json()["items"] == []
        assert c.post(f"/notifications/{item['id']}/read").status_code == 404
        state["email"] = "y@x.com"
        assert c.post(f"/notifications/{item['id']}/read").json() == {"status": "success", "unread_count": 0}
        assert c.get("/notifications?unread_only=true").json()["items"] == []
        assert c.post("/notifications/999999/read").status_code == 404
        assert c.post("/notifications/read-all").json()["unread_count"] == 0
        # logout: only the owner can unregister
        state["email"] = "o@x.com"
        assert c.post("/notifications/devices/unregister", json={"token": TOK(1)}).json()["status"] == "not_found"
        state["email"] = "y@x.com"
        assert c.post("/notifications/devices/unregister", json={"token": TOK(1)}).json()["status"] == "unregistered"
        # push failures never break activation
        hh.set_max_videos(S(), 5)
        push_fcm.send_to_token = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fcm down"))
        c.post("/notifications/devices", json={"token": TOK(3)})
        d2 = S(); d2.query(HappyHourSession).update({"status": "EXPIRED"}); d2.commit(); d2.close()
        d3 = S(); ent = hh.get_entitlement(d3, d3.query(User).filter_by(email="y@x.com").one().id)
        ent.last_token_activation_month = "2000-01"; d3.commit(); d3.close()
        assert c.post("/happy-hour/activate", headers={"Idempotency-Key": "k2"}).status_code == 200
    finally:
        push_fcm.send_to_token = _fake_send
        cleanup()


def test_10_http_admin_broadcast():
    c, S, state, _Fake, cleanup = _http_env(); nt.set_session_factory(S); reset_pushes()
    try:
        db = S(); month = hh.period_key(hh.utcnow()); now = hh.utcnow()
        admin = make_user(db, "admin@x.com", plan="Free"); admin.is_admin = True; db.commit()
        elig = make_user(db, "elig@x.com", cycle="yearly"); grant(db, elig, "yearly", "p1", now, sub_id="s1")
        fresh = make_user(db, "fresh@x.com", sub_id="s9")                                   # paying, no entitlement row yet
        used = make_user(db, "used@x.com", cycle="yearly", sub_id="s2"); grant(db, used, "yearly", "p2", now, sub_id="s2")
        hh.activate_token(db, used, "k", now=now - timedelta(days=40)) if False else None
        ent = hh.get_entitlement(db, used.id); ent.last_token_activation_month = month; db.commit()
        running = make_user(db, "run@x.com", sub_id="s3"); grant(db, running, "monthly", "p3", now, sub_id="s3")
        hh.activate_token(db, running, "k", now=now)
        free = make_user(db, "free@x.com", plan="Free", cycle="monthly")
        nt.register_device(db, elig.id, TOK(1)); nt.register_device(db, elig.id, TOK(2)); nt.register_device(db, free.id, TOK(3))
        dead = nt.register_device(db, fresh.id, TOK(4)); dead.disabled_at = now; db.commit()
        uid = {u.email: u.id for u in db.query(User).all()}
        db.close()
        body = {"title": "Happy Hour is live!", "body": "Use your token now.", "audience": "eligible"}

        state["email"] = "elig@x.com"
        assert c.post("/admin/notifications/happy-hour-broadcast", json=body).status_code == 403
        assert c.get("/admin/notifications/broadcasts").status_code == 403
        state["email"] = "admin@x.com"
        for bad in ({**body, "title": ""}, {**body, "title": "x" * 66}, {**body, "body": "y" * 241},
                    {**body, "audience": "everyone"}, {"title": "t"}):
            assert c.post("/admin/notifications/happy-hour-broadcast", json=bad).status_code == 422, bad

        # dry run: only counts, writes nothing
        r = c.post("/admin/notifications/happy-hour-broadcast", json={**body, "dry_run": True}).json()
        assert r == {"dry_run": True, "broadcast_id": None, "recipients": 2, "devices": 2}, r   # elig + fresh (no used/running/free)
        r = c.post("/admin/notifications/happy-hour-broadcast", json={**body, "audience": "all_subscribers", "dry_run": True}).json()
        assert r["recipients"] == 4, r                                                         # elig, fresh, used, running
        d = S(); assert d.query(Notification).count() == 0 and d.query(NotificationBroadcast).count() == 0; d.close()

        # real send
        r = c.post("/admin/notifications/happy-hour-broadcast", json=body).json()
        assert r["dry_run"] is False and r["recipients"] == 2 and r["devices"] == 2 and r["broadcast_id"]
        d = S()
        got = sorted(n.user_id for n in d.query(Notification).filter_by(type="HAPPY_HOUR_BROADCAST").all())
        assert got == sorted([uid["elig@x.com"], uid["fresh@x.com"]]), got
        assert sorted(p["token"] for p in PUSHES) == sorted([TOK(1), TOK(2)])                 # disabled token skipped
        assert all(p["data"]["type"] == "HAPPY_HOUR_BROADCAST" and p["title"] == body["title"] for p in PUSHES)
        d.close()
        hist = c.get("/admin/notifications/broadcasts").json()
        assert len(hist) == 1 and hist[0]["recipient_count"] == 2 and hist[0]["audience"] == "eligible"
        # a second broadcast is a new announcement (new dedupe key), not swallowed
        reset_pushes()
        assert c.post("/admin/notifications/happy-hour-broadcast", json=body).json()["recipients"] == 2
        d = S(); assert d.query(Notification).filter_by(user_id=uid["elig@x.com"]).count() == 2; d.close()
        # users see it in their bell
        state["email"] = "elig@x.com"
        items = c.get("/notifications").json()["items"]
        assert len(items) == 2 and items[0]["title"] == "Happy Hour is live!" and items[0]["data"]["broadcast_id"]
    finally:
        push_fcm.send_to_token = _fake_send
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
    for e in base._engines:
        e.dispose()
    shutil.rmtree(base.TMP, ignore_errors=True)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main_()
