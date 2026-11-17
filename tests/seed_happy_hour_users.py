"""
Local testing helper. Creates/resets verified users (with Happy Hour entitlements) for the web / Android UI.
Run:  python tests/seed_happy_hour_users.py        (safe to re-run: resets Happy Hour state + credits)

Refuses to run unless DATABASE_URL is a local SQLite file.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import timedelta
from database import SessionLocal, SQLALCHEMY_DATABASE_URL
from models import User, HappyHourSession, HappyHourEntitlement, Notification, DeviceToken, NotificationBroadcast
from auth import get_password_hash
import happy_hour as hh

if "sqlite" not in SQLALCHEMY_DATABASE_URL:
    sys.exit("Refusing to seed: DATABASE_URL is not a local SQLite database.")

PASSWORD = "Test@12345"
USERS = [
    # email,                      plan,   cycle,     credits, admin, note
    ("hh_monthly@example.com",   "Pro",  "monthly", 100, False, "1 token / billing period"),
    ("hh_yearly@example.com",    "Pro",  "yearly",  100, False, "4 tokens / year, 1 activation per calendar month"),
    ("hh_nocredits@example.com", "Pro",  "monthly", 0,   False, "0 credits: proves Happy Hour bypasses credits"),
    ("hh_free@example.com",      "Free", "monthly", 20,  False, "not eligible"),
    ("hh_admin@example.com",     "Free", "monthly", 20,  True,  "admin: PUT /admin/settings/happy-hour"),
]

db = SessionLocal()
try:
    now = hh.utcnow()
    for email, plan, cycle, credits, is_admin, _note in USERS:
        u = db.query(User).filter(User.email == email).first()
        if not u:
            u = User(email=email, full_name=email.split("@")[0], provider="local")
            db.add(u); db.flush()
        u.hashed_password = get_password_hash(PASSWORD)
        u.is_verified = True
        u.is_admin = is_admin
        u.plan, u.billing_cycle, u.subscription_status = plan, cycle, "active"
        u.subscription_id = f"sub_seed_{u.id}" if plan != "Free" else None
        u.credits = credits
        # reset Happy Hour state
        db.query(HappyHourSession).filter(HappyHourSession.user_id == u.id).delete()
        db.query(HappyHourEntitlement).filter(HappyHourEntitlement.user_id == u.id).delete()
        db.flush()
        # reset notifications + register a fake Android device (token is >= 20 chars like a real FCM token)
        db.query(Notification).filter(Notification.user_id == u.id).delete()
        db.query(DeviceToken).filter(DeviceToken.user_id == u.id).delete()
        db.query(DeviceToken).filter(DeviceToken.token == f"seed-device-{email}-0123456789").delete()
        db.add(DeviceToken(user_id=u.id, token=f"seed-device-{email}-0123456789", platform="android",
                           app_version="seed"))
        if plan != "Free":
            hh.grant_entitlement(db, u, cycle, u.subscription_id, f"seed-{u.id}-{now.isoformat()}",
                                 start=now, expiry=now + timedelta(days=366 if cycle == "yearly" else 32),
                                 source="bootstrap", now=now)
    db.query(NotificationBroadcast).delete()
    db.commit()
    print("Seeded (password for all:", PASSWORD + ")")
    for email, plan, cycle, credits, is_admin, note in USERS:
        print(f"  {email:28} {plan:5} {cycle:8} credits={credits:<4} {'ADMIN ' if is_admin else '      '}{note}")
finally:
    db.close()
