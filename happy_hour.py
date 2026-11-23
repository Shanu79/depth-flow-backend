"""
Happy Hour tokens: subscription-period entitlements + strict 60-minute unlimited-generation sessions.

Business rules (see CLAUDE.md for the full write-up)
  * Yearly workspace subscription : 4 tokens per subscription year. Unused tokens carry forward inside the
    year. At most ONE activation per UTC calendar month. Tokens end with the subscription year.
  * Monthly workspace subscription: 1 token per billing period, no rollover, re-granted by the renewal payment.
  * Activation consumes one token and opens a session of exactly 3600 s. `expires_at` is computed and stored
    on the server; the frontend countdown is display-only. One ACTIVE session per user (DB-enforced).
  * A session has a video cap (admin setting, snapshotted at activation). Generation uses a reserve ->
    confirm / release protocol so concurrent requests can never exceed the cap and failed videos never count.
    When the cap is reached the session becomes LIMIT_REACHED and requests fall back to normal credit logic.
  * Cancellation / expiry of the subscription invalidates the entitlement and any active session.
  * API-key subscriptions are NOT eligible (workspace plans only).
  * All timestamps are naive UTC (same convention as the rest of the backend).

Lifecycle functions (`grant_entitlement`, `invalidate_entitlement`) never commit: the billing handler that
calls them commits, so the subscription change and the token change land in one transaction.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import User, HappyHourEntitlement, HappyHourSession, AppSetting

HAPPY_HOUR_SECONDS = 3600  # exactly 60 minutes

YEARLY_TOKENS = 4
MONTHLY_TOKENS = 1

SETTING_MAX_VIDEOS = "happy_hour_max_videos"
DEFAULT_MAX_VIDEOS = 10
MAX_VIDEOS_CEILING = 1000

# session statuses
ACTIVE = "ACTIVE"
EXPIRED = "EXPIRED"
LIMIT_REACHED = "LIMIT_REACHED"
INVALIDATED = "INVALIDATED"
# entitlement statuses
ENT_ACTIVE = "ACTIVE"
ENT_CANCELLED = "CANCELLED"
ENT_EXPIRED = "EXPIRED"

# Plans that are NOT paid workspace subscriptions (new users are created as plan "Free" with status "active").
_NON_SUBSCRIPTION_PLANS = {"", "free", "trial", "credit pack"}

# Used only when the real period end is unknown (a cancel/expire event still ends the entitlement early).
_FALLBACK_PERIOD = {"monthly": timedelta(days=32), "yearly": timedelta(days=366)}


class HappyHourError(Exception):
    """`code` is a stable machine-readable identifier for the frontend / Android app."""
    def __init__(self, status_code: int, code: str, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


# ----------------------------------------------------------------------------- time helpers
def utcnow() -> datetime:
    return datetime.utcnow()


def period_key(now: datetime) -> str:
    """Calendar-month identifier, e.g. '2026-01'."""
    return f"{now.year:04d}-{now.month:02d}"


def next_month_start(now: datetime) -> datetime:
    """00:00:00 UTC on the 1st of the next calendar month."""
    if now.month == 12:
        return datetime(now.year + 1, 1, 1)
    return datetime(now.year, now.month + 1, 1)


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() + "Z" if dt else None


# ----------------------------------------------------------------------------- admin setting
def get_max_videos(db: Session) -> int:
    row = db.get(AppSetting, SETTING_MAX_VIDEOS)
    if row is not None:
        try:
            value = int(row.value)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return DEFAULT_MAX_VIDEOS


def set_max_videos(db: Session, value, admin_user_id: Optional[int] = None) -> int:
    """Validate (positive integer) and store. Affects only sessions activated afterwards."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Maximum videos per Happy Hour session must be an integer.")
    if value < 1 or value > MAX_VIDEOS_CEILING:
        raise ValueError(f"Maximum videos per Happy Hour session must be between 1 and {MAX_VIDEOS_CEILING}.")
    row = db.get(AppSetting, SETTING_MAX_VIDEOS)
    if row is None:
        row = AppSetting(key=SETTING_MAX_VIDEOS, value=str(value))
        db.add(row)
    row.value = str(value)
    row.updated_by_user_id = admin_user_id
    row.updated_at = utcnow()
    db.commit()
    return value


# ----------------------------------------------------------------------------- subscription helpers
def _subscription_is_active(status: Optional[str]) -> bool:
    s = (status or "").strip().lower()
    # "Scheduled for cancellation on <date>" is still a paid, active subscription until that date.
    return s == "active" or s.startswith("scheduled for cancellation")


def subscription_type_of(user: User) -> Optional[str]:
    """'monthly' | 'yearly' for an active paid WORKSPACE subscription, else None. API plans never qualify."""
    if not _subscription_is_active(user.subscription_status):
        return None
    if (user.plan or "").strip().lower() in _NON_SUBSCRIPTION_PLANS:
        return None
    cycle = (user.billing_cycle or "").strip().lower()
    return cycle if cycle in ("monthly", "yearly") else None


def tokens_for(subscription_type: str) -> int:
    return YEARLY_TOKENS if subscription_type == "yearly" else MONTHLY_TOKENS


# ----------------------------------------------------------------------------- entitlement lifecycle
def get_entitlement(db: Session, user_id: int) -> Optional[HappyHourEntitlement]:
    return db.query(HappyHourEntitlement).filter(HappyHourEntitlement.user_id == user_id).first()


def grant_entitlement(db: Session, user: User, subscription_type: str, subscription_id: Optional[str],
                      grant_ref: Optional[str], start: Optional[datetime] = None,
                      expiry: Optional[datetime] = None, source: str = "dodo",
                      now: Optional[datetime] = None) -> Optional[HappyHourEntitlement]:
    """
    Give the user the token allocation for a new / renewed subscription period. Does NOT commit.
      yearly  -> total 4, available 4  (new year: everything is replaced, nothing stacks)
      monthly -> total 1, available 1  (an unused previous token is discarded: no rollover)
    Idempotent on `grant_ref` (payment id / order id): replays of the same payment are no-ops, so duplicate
    webhooks or retries can never duplicate tokens.
    `last_token_activation_month` is preserved so re-buying cannot bypass "1 activation per calendar month".
    """
    if subscription_type not in ("monthly", "yearly"):
        return None
    now = now or utcnow()
    ent = get_entitlement(db, user.id)
    if ent is not None and grant_ref and ent.last_grant_ref == grant_ref:
        return ent

    start = start or now
    if expiry is None or expiry <= now:
        expiry = start + _FALLBACK_PERIOD[subscription_type]
    if ent is None:
        ent = HappyHourEntitlement(user_id=user.id, last_token_activation_month=None)
        db.add(ent)

    total = tokens_for(subscription_type)
    ent.subscription_id = subscription_id
    ent.subscription_type = subscription_type
    ent.source = source
    ent.total_allocated_tokens = total
    ent.available_tokens = total
    ent.total_claimed_tokens = 0
    ent.subscription_start_date = start
    ent.subscription_expiry_date = expiry
    ent.entitlement_status = ENT_ACTIVE
    ent.last_grant_ref = grant_ref
    ent.updated_at = now
    db.flush()
    return ent


def invalidate_entitlement(db: Session, user: User, status: str = ENT_CANCELLED,
                           now: Optional[datetime] = None) -> bool:
    """
    Subscription cancelled / expired: unused tokens are void and any running session is INVALIDATED.
    Does NOT commit. Returns True if anything changed.
    """
    now = now or utcnow()
    changed = False
    ent = get_entitlement(db, user.id)
    if ent is not None and (ent.entitlement_status == ENT_ACTIVE or ent.available_tokens):
        ent.available_tokens = 0
        ent.entitlement_status = status
        ent.updated_at = now
        changed = True
    n = db.execute(
        update(HappyHourSession)
        .where(HappyHourSession.user_id == user.id, HappyHourSession.status == ACTIVE)
        .values(status=INVALIDATED, updated_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount
    return changed or bool(n)


def ensure_entitlement(db: Session, user: User, now: Optional[datetime] = None) -> Optional[HappyHourEntitlement]:
    """
    Current entitlement for the user, with lazy housekeeping (does not commit):
      * entitlement past its expiry date -> EXPIRED (tokens void, session invalidated)
      * already-paying user who has no row yet (subscribed before this feature) -> one-time bootstrap grant
    """
    now = now or utcnow()
    ent = get_entitlement(db, user.id)
    if ent is not None:
        if (ent.entitlement_status == ENT_ACTIVE and ent.subscription_expiry_date
                and now >= ent.subscription_expiry_date):
            invalidate_entitlement(db, user, ENT_EXPIRED, now)
        return ent

    stype = subscription_type_of(user)
    if stype is None:
        return None
    end = user.subscription_end_date if (user.subscription_end_date and user.subscription_end_date > now) else None
    ref = f"bootstrap:{user.subscription_id or user.play_purchase_token or user.id}"
    return grant_entitlement(db, user, stype, user.subscription_id or user.play_purchase_token, ref,
                             start=now, expiry=end, source="bootstrap", now=now)


def _entitlement_usable(user: User, ent: Optional[HappyHourEntitlement], now: datetime) -> bool:
    return (
        ent is not None
        and ent.entitlement_status == ENT_ACTIVE
        and subscription_type_of(user) is not None
        and (ent.subscription_expiry_date is None or now < ent.subscription_expiry_date)
    )


# ----------------------------------------------------------------------------- sessions
def _active_session(db: Session, user_id: int) -> Optional[HappyHourSession]:
    return (
        db.query(HappyHourSession)
        .filter(HappyHourSession.user_id == user_id, HappyHourSession.status == ACTIVE)
        .first()
    )


def _expire_if_due(session: Optional[HappyHourSession], now: datetime) -> bool:
    if session is not None and session.status == ACTIVE and session.expires_at <= now:
        session.status = EXPIRED
        session.updated_at = now
        return True
    return False


def get_active_session(db: Session, user: User, now: Optional[datetime] = None) -> Optional[HappyHourSession]:
    """The user's live session. A session past its server-side expiry is marked EXPIRED (and committed)."""
    now = now or utcnow()
    session = _active_session(db, user.id)
    if _expire_if_due(session, now):
        db.commit()
        return None
    return session


def _fail(db: Session, status_code: int, code: str, detail: str):
    db.commit()  # persist lazy housekeeping (expiry flips, bootstrap) even though the request is refused
    raise HappyHourError(status_code, code, detail)


def activate_token(db: Session, user: User, idempotency_key: Optional[str] = None,
                   now: Optional[datetime] = None) -> Tuple[HappyHourSession, bool]:
    """
    Atomically: validate -> deduct exactly one token -> create the 60-minute session (single commit).
    Returns (session, replayed). `replayed` is True when the same idempotency key was already used.
    """
    now = now or utcnow()

    # Row lock on the user serialises concurrent activations on Postgres (no-op on SQLite, where the
    # partial unique index on ACTIVE sessions plus the entitlement CHECKs are the backstop).
    locked = (
        db.query(User).filter(User.id == user.id)
        .populate_existing().with_for_update().one()
    )

    if idempotency_key:
        prior = (
            db.query(HappyHourSession)
            .filter(HappyHourSession.user_id == locked.id, HappyHourSession.idempotency_key == idempotency_key)
            .first()
        )
        if prior is not None:
            _expire_if_due(prior, now)
            db.commit()
            return prior, True

    active = _active_session(db, locked.id)
    if _expire_if_due(active, now):
        active = None
    if active is not None:
        _fail(db, 409, "SESSION_ACTIVE", "A Happy Hour session is already active.")

    # 1. subscription must be valid and active
    ent = ensure_entitlement(db, locked, now)
    if not _entitlement_usable(locked, ent, now):
        _fail(db, 403, "NOT_SUBSCRIBED",
              "An active yearly or monthly subscription is required to use a Happy Hour token.")
    # 2. token available
    if ent.available_tokens <= 0:
        _fail(db, 403, "NO_TOKENS", "You have no Happy Hour tokens left for this subscription period.")
    # 3. max one activation per calendar month
    month = period_key(now)
    if ent.last_token_activation_month == month:
        _fail(db, 403, "MONTHLY_LIMIT",
              "You have already activated a Happy Hour token this calendar month. "
              f"Next activation available on {iso(next_month_start(now))}.")

    # 4-9. deduct + create session, one transaction
    ent.available_tokens -= 1
    ent.total_claimed_tokens += 1
    ent.last_token_activation_month = month
    ent.updated_at = now

    session = HappyHourSession(
        user_id=locked.id,
        entitlement_id=ent.id,
        subscription_id=ent.subscription_id,
        status=ACTIVE,
        started_at=now,
        expires_at=now + timedelta(seconds=HAPPY_HOUR_SECONDS),
        max_video_generations=get_max_videos(db),   # snapshot: later admin changes don't touch this session
        successful_video_generations=0,
        in_flight_generations=0,
        idempotency_key=idempotency_key,
        created_at=now,
        updated_at=now,
    )
    db.add(session)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()  # token deduction rolls back together with the failed session insert
        if idempotency_key:
            prior = (
                db.query(HappyHourSession)
                .filter(HappyHourSession.user_id == user.id, HappyHourSession.idempotency_key == idempotency_key)
                .first()
            )
            if prior is not None:
                return prior, True
        raise HappyHourError(409, "SESSION_ACTIVE", "A Happy Hour session is already active.")
    db.refresh(session)
    return session, False


# ----------------------------------------------------------------------------- generation gate
@dataclass(frozen=True)
class GenerationGrant:
    session_id: int
    user_id: int


def begin_generation(db: Session, user: User, now: Optional[datetime] = None) -> Optional[GenerationGrant]:
    """
    Validation order for every generation request:
      1. subscription valid + active (and entitlement usable)
      2. an ACTIVE session exists (a stale one is marked EXPIRED and ignored)
      3. server time < expires_at, and a video slot is free
    Returns a grant (one video slot reserved, committed) -> caller skips credit checks/deduction, then calls
    confirm_generation() in its final commit or release_generation() on failure.
    Returns None -> caller continues with the normal credit logic (no session / expired / sub inactive /
    cap reached). Nothing here trusts the frontend.
    """
    now = now or utcnow()
    session = get_active_session(db, user, now)
    if session is None:
        return None

    ent = get_entitlement(db, user.id)
    if not _entitlement_usable(user, ent, now):
        return None

    # Atomic reservation: the WHERE clause is the concurrency guard (cannot exceed the cap).
    reserved = db.execute(
        update(HappyHourSession)
        .where(
            HappyHourSession.id == session.id,
            HappyHourSession.status == ACTIVE,
            HappyHourSession.expires_at > now,
            HappyHourSession.successful_video_generations + HappyHourSession.in_flight_generations
            < HappyHourSession.max_video_generations,
        )
        .values(in_flight_generations=HappyHourSession.in_flight_generations + 1, updated_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount
    if reserved == 1:
        db.commit()
        return GenerationGrant(session_id=session.id, user_id=user.id)

    db.rollback()
    # Cap fully consumed by finished videos -> close the session as LIMIT_REACHED.
    db.execute(
        update(HappyHourSession)
        .where(HappyHourSession.id == session.id, HappyHourSession.status == ACTIVE,
               HappyHourSession.successful_video_generations >= HappyHourSession.max_video_generations)
        .values(status=LIMIT_REACHED, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return None


def confirm_generation(db: Session, grant: GenerationGrant, now: Optional[datetime] = None) -> None:
    """A video was successfully created: slot reserved -> used. Does NOT commit (rides the caller's commit)."""
    now = now or utcnow()
    db.execute(
        update(HappyHourSession)
        .where(HappyHourSession.id == grant.session_id)
        .values(in_flight_generations=HappyHourSession.in_flight_generations - 1,
                successful_video_generations=HappyHourSession.successful_video_generations + 1,
                updated_at=now)
        .execution_options(synchronize_session=False)
    )
    db.execute(
        update(HappyHourSession)
        .where(HappyHourSession.id == grant.session_id, HappyHourSession.status == ACTIVE,
               HappyHourSession.successful_video_generations >= HappyHourSession.max_video_generations)
        .values(status=LIMIT_REACHED, updated_at=now)
        .execution_options(synchronize_session=False)
    )


def release_generation(db: Session, grant: GenerationGrant, now: Optional[datetime] = None) -> None:
    """The generation failed: give the slot back (failed videos never count). Commits."""
    now = now or utcnow()
    try:
        db.rollback()
        db.execute(
            update(HappyHourSession)
            .where(HappyHourSession.id == grant.session_id, HappyHourSession.in_flight_generations > 0)
            .values(in_flight_generations=HappyHourSession.in_flight_generations - 1, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        db.commit()
    except Exception:
        db.rollback()


def remaining_videos(session: Optional[HappyHourSession]) -> int:
    if session is None or session.status != ACTIVE:
        return 0
    return max(0, session.max_video_generations - session.successful_video_generations
               - session.in_flight_generations)


# ----------------------------------------------------------------------------- serialisation
def serialize_session(s: HappyHourSession, now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    remaining_seconds = 0
    if s.status == ACTIVE:
        remaining_seconds = max(0, int((s.expires_at - now).total_seconds()))
    return {
        "id": s.id,
        "status": s.status,
        "subscription_id": s.subscription_id,
        "started_at": iso(s.started_at),
        "expires_at": iso(s.expires_at),
        "remaining_seconds": remaining_seconds,
        "max_video_generations": s.max_video_generations,
        "successful_video_generations": s.successful_video_generations,
        "remaining_video_allowance": remaining_videos(s),
        "created_at": iso(s.created_at),
        "updated_at": iso(s.updated_at),
    }


def build_status(db: Session, user: User, now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    session = get_active_session(db, user, now)
    ent = ensure_entitlement(db, user, now)
    db.commit()  # persist any lazy expiry / bootstrap
    if session is not None and session.status != ACTIVE:  # invalidated by the housekeeping above
        session = None

    sub_type = subscription_type_of(user)
    usable = _entitlement_usable(user, ent, now)
    this_month = period_key(now)

    blocked = None
    if not usable:
        blocked = "NOT_SUBSCRIBED"
    elif session is not None:
        blocked = "SESSION_ACTIVE"
    elif ent.available_tokens <= 0:
        blocked = "NO_TOKENS"
    elif ent.last_token_activation_month == this_month:
        blocked = "MONTHLY_LIMIT"

    return {
        "server_time": iso(now),  # anchor the countdown to this, not the device clock
        "eligible": usable,
        "subscription_type": ent.subscription_type if usable else sub_type,
        "total_allocated_tokens": ent.total_allocated_tokens if usable else 0,
        "available_tokens": ent.available_tokens if usable else 0,
        "total_claimed_tokens": ent.total_claimed_tokens if usable else 0,
        "last_token_activation_month": ent.last_token_activation_month if ent else None,
        "can_activate_this_month": blocked is None,
        "blocked_reason": blocked,  # NOT_SUBSCRIBED | SESSION_ACTIVE | NO_TOKENS | MONTHLY_LIMIT | null
        "next_activation_available_at": iso(next_month_start(now)) if blocked == "MONTHLY_LIMIT" else None,
        "entitlement_expires_at": iso(ent.subscription_expiry_date) if usable else None,
        "session_duration_seconds": HAPPY_HOUR_SECONDS,
        "active": session is not None,
        "session": serialize_session(session, now) if session is not None else None,
        "remaining_video_allowance": remaining_videos(session),
    }


def build_session_info(db: Session, user: User, now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    session = get_active_session(db, user, now)
    if session is None:
        return {"server_time": iso(now), "active": False, "session": None}
    return {"server_time": iso(now), "active": True, "session": serialize_session(session, now)}
