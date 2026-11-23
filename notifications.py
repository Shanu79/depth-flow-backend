"""
Happy Hour notifier: in-app inbox (website bell + Android list) + FCM push for Android.

Design rules
  * The inbox table is the source of truth. A push is sent ONLY when a new inbox row was created, and every
    event has a dedupe key (unique per user), so webhook retries / overlapping sweeps never notify twice.
  * Nothing here may break the caller: all push work is off the request path and every error is logged.
  * `notify()` commits. Call it AFTER the business transaction has been committed.
  * All timestamps are naive UTC, serialised as ISO-8601 with a trailing "Z" (same as the rest of the API).

Trigger map
  token granted (purchase/renewal) ... notify_token_granted()     called from payments_router._grant_happy_hour
  session live ..................... notify_session_started()     called from POST /happy-hour/activate
  ending soon / ended / month unlock  run_notification_sweep()     in-process loop (main.py) + cron_happy_hour_notify.py
  admin broadcast .................. send_happy_hour_broadcast()  called from POST /admin/notifications/...
"""
import json
import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import happy_hour as hh
import push_fcm
from database import SessionLocal
from models import (DeviceToken, HappyHourEntitlement, HappyHourSession, Notification,
                    NotificationBroadcast, User)

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------- constants
HAPPY_HOUR_TOKEN_AVAILABLE = "HAPPY_HOUR_TOKEN_AVAILABLE"
HAPPY_HOUR_STARTED = "HAPPY_HOUR_STARTED"
HAPPY_HOUR_ENDING_SOON = "HAPPY_HOUR_ENDING_SOON"
HAPPY_HOUR_ENDED = "HAPPY_HOUR_ENDED"
HAPPY_HOUR_BROADCAST = "HAPPY_HOUR_BROADCAST"

ENDING_SOON_MINUTES = 10
ENDED_LOOKBACK = timedelta(hours=24)
ROUTE_HAPPY_HOUR = "happy_hour"          # Android/web deep-link hint (data.route)
BROADCAST_AUDIENCES = ("eligible", "all_subscribers")

# ----------------------------------------------------------------------------- push plumbing
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="push")
_session_factory: Callable[[], Session] = SessionLocal


def set_session_factory(factory: Callable[[], Session]) -> None:
    """Tests point the push worker (dead-token pruning) and the sweep loop at a throw-away DB."""
    global _session_factory
    _session_factory = factory


def _push_sync() -> bool:
    return os.getenv("NOTIFY_PUSH_SYNC", "").strip().lower() in ("1", "true", "yes", "on")


def _submit(fn, *args) -> None:
    def _safe():
        try:
            fn(*args)
        except Exception as e:
            logger.warning("push job failed: %s", e)

    if _push_sync():
        _safe()
    else:
        _executor.submit(_safe)


def _push_job(tokens: List[str], title: str, body: str, data: Dict[str, object]) -> None:
    result = push_fcm.send_to_tokens(tokens, title, body, data)
    if result["dead"]:
        db = _session_factory()
        try:
            now = hh.utcnow()
            db.query(DeviceToken).filter(DeviceToken.token.in_(result["dead"])).update(
                {DeviceToken.disabled_at: now}, synchronize_session=False)
            db.commit()
        except Exception as e:
            db.rollback()
            logger.warning("could not disable dead device tokens: %s", e)
        finally:
            db.close()


def _active_tokens(db: Session, user_ids: List[int]) -> Dict[int, List[str]]:
    out: Dict[int, List[str]] = {}
    if not user_ids:
        return out
    for uid, token in (db.query(DeviceToken.user_id, DeviceToken.token)
                       .filter(DeviceToken.user_id.in_(user_ids), DeviceToken.disabled_at.is_(None)).all()):
        out.setdefault(uid, []).append(token)
    return out


# ----------------------------------------------------------------------------- serialisation
def serialize_notification(n: Notification) -> dict:
    try:
        data = json.loads(n.data) if n.data else {}
    except ValueError:
        data = {}
    return {
        "id": n.id,
        "type": n.type,
        "title": n.title,
        "body": n.body,
        "data": data,
        "created_at": hh.iso(n.created_at),
        "read_at": hh.iso(n.read_at),
    }


# ----------------------------------------------------------------------------- core: create + push
def notify(db: Session, user_id: int, type_: str, title: str, body: str,
           data: Optional[Dict[str, object]] = None, dedupe_key: Optional[str] = None,
           now: Optional[datetime] = None) -> Optional[Notification]:
    """
    Create the inbox row (idempotent on dedupe_key) and push it to the user's Android devices.
    Returns the new Notification, or None if it already existed / could not be stored. Never raises.
    Commits: call it after the business transaction.
    """
    try:
        now = now or hh.utcnow()
        data = dict(data or {})
        data.setdefault("route", ROUTE_HAPPY_HOUR)
        key = dedupe_key or f"{type_}:{now.isoformat()}"

        exists = (db.query(Notification.id)
                  .filter(Notification.user_id == user_id, Notification.dedupe_key == key).first())
        if exists:
            return None

        n = Notification(user_id=user_id, type=type_, title=title, body=body, data=json.dumps(data),
                         dedupe_key=key, created_at=now)
        db.add(n)
        try:
            db.commit()
        except IntegrityError:        # lost a race with another worker: it already notified
            db.rollback()
            return None
        db.refresh(n)

        tokens = _active_tokens(db, [user_id]).get(user_id, [])
        if tokens:
            payload = {"type": type_, "notification_id": n.id, **data}
            _submit(_push_job, tokens, title, body, payload)
        return n
    except Exception as e:
        logger.warning("notify failed (%s): %s", type_, e)
        try:
            db.rollback()
        except Exception:
            pass
        return None


# ----------------------------------------------------------------------------- inbox queries
def list_notifications(db: Session, user_id: int, limit: int = 20, after_id: Optional[int] = None,
                       before_id: Optional[int] = None, unread_only: bool = False) -> List[Notification]:
    q = db.query(Notification).filter(Notification.user_id == user_id)
    if after_id is not None:
        q = q.filter(Notification.id > after_id)
    if before_id is not None:
        q = q.filter(Notification.id < before_id)
    if unread_only:
        q = q.filter(Notification.read_at.is_(None))
    return q.order_by(Notification.id.desc()).limit(limit).all()


def unread_count(db: Session, user_id: int) -> int:
    return db.query(Notification).filter(Notification.user_id == user_id, Notification.read_at.is_(None)).count()


def mark_read(db: Session, user_id: int, notification_id: int) -> bool:
    """False if the notification does not exist OR belongs to someone else."""
    n = (db.query(Notification)
         .filter(Notification.id == notification_id, Notification.user_id == user_id).first())
    if n is None:
        return False
    if n.read_at is None:
        n.read_at = hh.utcnow()
        db.commit()
    return True


def mark_all_read(db: Session, user_id: int) -> int:
    n = (db.query(Notification)
         .filter(Notification.user_id == user_id, Notification.read_at.is_(None))
         .update({Notification.read_at: hh.utcnow()}, synchronize_session=False))
    db.commit()
    return n


# ----------------------------------------------------------------------------- devices
def register_device(db: Session, user_id: int, token: str, platform: str = "android",
                    app_version: Optional[str] = None) -> DeviceToken:
    """Upsert. A token re-registered by a different user moves to them (shared phone / re-login)."""
    now = hh.utcnow()
    dev = db.query(DeviceToken).filter(DeviceToken.token == token).first()
    if dev is None:
        dev = DeviceToken(user_id=user_id, token=token, platform=platform, app_version=app_version,
                          created_at=now, last_seen_at=now)
        db.add(dev)
    else:
        dev.user_id = user_id
        dev.platform = platform
        dev.app_version = app_version or dev.app_version
        dev.last_seen_at = now
        dev.disabled_at = None
    try:
        db.commit()
    except IntegrityError:            # concurrent first registration of the same token
        db.rollback()
        dev = db.query(DeviceToken).filter(DeviceToken.token == token).one()
        dev.user_id = user_id
        dev.disabled_at = None
        db.commit()
    db.refresh(dev)
    return dev


def unregister_device(db: Session, user_id: int, token: str) -> bool:
    """Only the owner can remove a token (logout). Returns True if something was removed."""
    n = db.query(DeviceToken).filter(DeviceToken.token == token, DeviceToken.user_id == user_id).delete()
    db.commit()
    return bool(n)


# ----------------------------------------------------------------------------- event helpers
def notify_token_granted(db: Session, user_id: int, ent: HappyHourEntitlement, grant_ref: Optional[str]):
    if ent.subscription_type == "yearly":
        title = "Your Happy Hour tokens are ready"
        body = (f"You have {ent.available_tokens} Happy Hour tokens this year. Activate one for "
                "60 minutes of unlimited generation (one per calendar month).")
    else:
        title = "Your Happy Hour token is ready"
        body = "Activate it before your billing period ends for 60 minutes of unlimited generation."
    return notify(db, user_id, HAPPY_HOUR_TOKEN_AVAILABLE, title, body,
                  {"subscription_type": ent.subscription_type, "available_tokens": ent.available_tokens},
                  dedupe_key=f"token-grant:{grant_ref or ent.id}")


def notify_session_started(db: Session, session: HappyHourSession):
    return notify(
        db, session.user_id, HAPPY_HOUR_STARTED, "Happy Hour is live!",
        f"Unlimited generation for the next 60 minutes (up to {session.max_video_generations} videos).",
        {"session_id": session.id, "expires_at": hh.iso(session.expires_at),
         "max_video_generations": session.max_video_generations},
        dedupe_key=f"session-start:{session.id}", now=session.started_at)


def _ended_text(status: str, max_videos: int):
    if status == hh.LIMIT_REACHED:
        return ("Happy Hour video limit reached",
                f"You used all {max_videos} free videos. Normal credits apply for the rest of the hour.")
    if status == hh.INVALIDATED:
        return ("Happy Hour has ended", "Your subscription is no longer active, so Happy Hour was stopped.")
    return ("Happy Hour has ended", "Normal credit-based generation has resumed.")


# ----------------------------------------------------------------------------- the sweep
def run_notification_sweep(db: Session, now: Optional[datetime] = None) -> Dict[str, int]:
    """
    Time-based notices. Idempotent (dedupe keys), safe to run from several workers / cron at once.
    Order matters: expire due sessions first so "ended" and "ending soon" never overlap.
    """
    now = now or hh.utcnow()
    counts = {"expired": 0, "ended": 0, "ending_soon": 0, "month_unlock": 0}

    # 1. sessions whose server-side expiry has passed -> EXPIRED
    due = (db.query(HappyHourSession)
           .filter(HappyHourSession.status == hh.ACTIVE, HappyHourSession.expires_at <= now).all())
    for s in due:
        s.status = hh.EXPIRED
        s.updated_at = now
    if due:
        db.commit()
    counts["expired"] = len(due)

    # 2. ended notices (time ran out / video cap / subscription cancelled)
    ended = (db.query(HappyHourSession)
             .filter(HappyHourSession.status != hh.ACTIVE, HappyHourSession.updated_at >= now - ENDED_LOOKBACK)
             .all())
    # A session whose time ran out long ago (sweep was down) is not worth a stale "ended" push.
    ended_rows = [(s.id, s.user_id, s.status, s.max_video_generations, s.successful_video_generations,
                   s.expires_at) for s in ended
                  if not (s.status == hh.EXPIRED and now - s.expires_at > ENDED_LOOKBACK)]
    for sid, uid, status, max_v, used, expires_at in ended_rows:
        title, body = _ended_text(status, max_v)
        if notify(db, uid, HAPPY_HOUR_ENDED, title, body,
                  {"session_id": sid, "status": status, "videos_used": used},
                  dedupe_key=f"session-end:{sid}", now=now):
            counts["ended"] += 1

    # 3. ending soon
    soon = (db.query(HappyHourSession)
            .filter(HappyHourSession.status == hh.ACTIVE, HappyHourSession.expires_at > now,
                    HappyHourSession.expires_at <= now + timedelta(minutes=ENDING_SOON_MINUTES)).all())
    soon_rows = [(s.id, s.user_id, s.expires_at, hh.remaining_videos(s)) for s in soon]
    for sid, uid, expires_at, remaining in soon_rows:
        minutes = max(1, math.ceil((expires_at - now).total_seconds() / 60))
        if notify(db, uid, HAPPY_HOUR_ENDING_SOON, "Happy Hour ends soon",
                  f"About {minutes} minute{'s' if minutes != 1 else ''} left and {remaining} video"
                  f"{'s' if remaining != 1 else ''} remaining.",
                  {"session_id": sid, "expires_at": hh.iso(expires_at), "remaining_videos": remaining},
                  dedupe_key=f"session-soon:{sid}", now=now):
            counts["ending_soon"] += 1

    # 4. yearly users: a new calendar month reopens their one-per-month activation
    month = hh.period_key(now)
    candidates = (db.query(HappyHourEntitlement)
                  .filter(HappyHourEntitlement.entitlement_status == hh.ENT_ACTIVE,
                          HappyHourEntitlement.subscription_type == "yearly",
                          HappyHourEntitlement.available_tokens > 0).all())
    for ent in candidates:
        if ent.last_token_activation_month == month:
            continue
        if ent.subscription_start_date and hh.period_key(ent.subscription_start_date) == month:
            continue                       # granted this month: the "token ready" notice already covered it
        if ent.subscription_expiry_date and ent.subscription_expiry_date <= now:
            continue
        user = db.get(User, ent.user_id)
        if user is None or not hh._entitlement_usable(user, ent, now):
            continue
        if hh._active_session(db, user.id) is not None:
            continue
        left = ent.available_tokens
        if notify(db, user.id, HAPPY_HOUR_TOKEN_AVAILABLE, "Happy Hour is available this month",
                  f"You have {left} token{'s' if left != 1 else ''} left. Activate one for 60 minutes of "
                  "unlimited generation.",
                  {"available_tokens": left, "month": month},
                  dedupe_key=f"month-unlock:{user.id}:{month}", now=now):
            counts["month_unlock"] += 1

    return counts


def run_sweep_once() -> Dict[str, int]:
    """Open a session, sweep, close. Used by the in-process loop and the cron script."""
    db = _session_factory()
    try:
        return run_notification_sweep(db)
    except Exception as e:
        db.rollback()
        logger.error("notification sweep failed: %s", e)
        return {}
    finally:
        db.close()


# ----------------------------------------------------------------------------- admin broadcast
def _broadcast_audience(db: Session, audience: str, now: datetime) -> List[int]:
    """User ids that should receive the broadcast."""
    month = hh.period_key(now)
    active_session_users = {r[0] for r in db.query(HappyHourSession.user_id)
                            .filter(HappyHourSession.status == hh.ACTIVE).all()}
    ents = {e.user_id: e for e in db.query(HappyHourEntitlement).all()}
    ids: List[int] = []
    for user in db.query(User).yield_per(500):
        if hh.subscription_type_of(user) is None:
            continue
        if audience == "eligible":
            if user.id in active_session_users:
                continue
            ent = ents.get(user.id)
            # a paying user without an entitlement row yet is bootstrapped on first use -> counts as eligible
            if ent is not None and not (hh._entitlement_usable(user, ent, now) and ent.available_tokens > 0
                                        and ent.last_token_activation_month != month):
                continue
        ids.append(user.id)
    return ids


def send_happy_hour_broadcast(db: Session, admin_id: Optional[int], title: str, body: str,
                              audience: str = "eligible", dry_run: bool = False,
                              now: Optional[datetime] = None) -> dict:
    now = now or hh.utcnow()
    if audience not in BROADCAST_AUDIENCES:
        raise ValueError(f"audience must be one of {', '.join(BROADCAST_AUDIENCES)}")
    user_ids = _broadcast_audience(db, audience, now)
    tokens_by_user = _active_tokens(db, user_ids)
    device_count = sum(len(v) for v in tokens_by_user.values())

    if dry_run:
        return {"dry_run": True, "broadcast_id": None, "recipients": len(user_ids), "devices": device_count}

    bc = NotificationBroadcast(title=title, body=body, audience=audience, created_by_user_id=admin_id,
                               recipient_count=len(user_ids), device_count=device_count, created_at=now)
    db.add(bc)
    db.commit()
    db.refresh(bc)

    key = f"broadcast:{bc.id}"
    data = {"route": ROUTE_HAPPY_HOUR, "broadcast_id": bc.id}
    for i in range(0, len(user_ids), 500):
        chunk = user_ids[i:i + 500]
        rows = [Notification(user_id=uid, type=HAPPY_HOUR_BROADCAST, title=title, body=body,
                             data=json.dumps(data), dedupe_key=key, created_at=now) for uid in chunk]
        db.add_all(rows)
        db.flush()                                    # assigns ids without expiring the rows
        pairs = [(r.user_id, r.id) for r in rows]
        db.commit()
        for uid, nid in pairs:
            tokens = tokens_by_user.get(uid)
            if tokens:
                payload = {"type": HAPPY_HOUR_BROADCAST, "notification_id": nid, **data}
                _submit(_push_job, tokens, title, body, payload)
    return {"dry_run": False, "broadcast_id": bc.id, "recipients": len(user_ids), "devices": device_count}


def list_broadcasts(db: Session, limit: int = 50) -> List[dict]:
    rows = db.query(NotificationBroadcast).order_by(NotificationBroadcast.id.desc()).limit(limit).all()
    return [{"id": b.id, "title": b.title, "body": b.body, "audience": b.audience,
             "created_by_user_id": b.created_by_user_id, "recipient_count": b.recipient_count,
             "device_count": b.device_count, "created_at": hh.iso(b.created_at)} for b in rows]
