from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Index, UniqueConstraint, CheckConstraint, Text, text
from sqlalchemy.orm import relationship
from database import Base
from datetime import datetime, timezone

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String, nullable=True)
    full_name = Column(String, nullable=True)
    profile_pic = Column(String, nullable=True)
    provider = Column(String, default="local")
    
    # --- WORKSPACE / PLATFORM SUBSCRIPTION ---
    plan = Column(String, default="free") 
    billing_cycle = Column(String, nullable=True) 
    subscription_id = Column(String, nullable=True) 
    subscription_status = Column(String, default="inactive") 
    subscription_end_date = Column(DateTime, nullable=True)
    last_payment_id = Column(String, nullable=True)
    
    # --- GOOGLE PLAY BILLING ---
    # ADDED THIS: Used to link background webhook renewals to this user
    play_purchase_token = Column(String, index=True, nullable=True) 
    
    # --- API SUBSCRIPTION (NEW FIELDS) ---
    api_plan = Column(String, default="free") # "Free", "Api-Pro", etc.
    api_billing_cycle = Column(String, nullable=True) 
    api_subscription_id = Column(String, nullable=True) 
    api_subscription_status = Column(String, default="inactive") 
    api_subscription_end_date = Column(DateTime, nullable=True) 
    
    # THE LEDGER (Shared between platform and API)
    credits = Column(Integer, default=20)
    last_login_ip = Column(String, nullable=True)
    is_admin = Column(Boolean, default=False)
    
    api_key = Column(String, unique=True, index=True, nullable=True)
    
    # Inside your User class in models.py add:
    is_verified = Column(Boolean, default=False)
    otp = Column(String, nullable=True)
    otp_expiry = Column(DateTime, nullable=True)
    
    # --- ADD THIS NEW FIELD ---
    next_credit_drop_date = Column(DateTime, nullable=True)


class GenerationHistory(Base):
    __tablename__ = "generation_history"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    video_url = Column(String, nullable=False)
    
    # FIXED: Use timezone-aware UTC default
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc)) 
    
    source = Column(String, default="workspace")
    # Relationship to user
    owner = relationship("User", back_populates="generations")

# Update User class to include the relationship
User.generations = relationship("GenerationHistory", back_populates="owner")



# =====================================================================
# HAPPY HOUR (all timestamps are naive UTC, like the rest of the app)
# =====================================================================
class HappyHourEntitlement(Base):
    """
    The Happy Hour tokens attached to a user's current workspace subscription period.
    One row per user; replaced (never stacked) by each grant (new purchase / renewal).
      yearly  -> 4 tokens per subscription year, carry forward, max 1 activation per calendar month
      monthly -> 1 token per billing period, no rollover (re-granted on renewal)
    """
    __tablename__ = "happy_hour_entitlements"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    subscription_id = Column(String, nullable=True)
    subscription_type = Column(String, nullable=False)           # monthly | yearly
    source = Column(String, nullable=False, default="dodo")      # dodo | google_play | bootstrap
    total_allocated_tokens = Column(Integer, nullable=False, default=0)
    available_tokens = Column(Integer, nullable=False, default=0)
    total_claimed_tokens = Column(Integer, nullable=False, default=0)
    last_token_activation_month = Column(String, nullable=True)  # "YYYY-MM" UTC of the last activation
    subscription_start_date = Column(DateTime, nullable=True)
    subscription_expiry_date = Column(DateTime, nullable=True)
    entitlement_status = Column(String, nullable=False, default="ACTIVE")  # ACTIVE | CANCELLED | EXPIRED
    last_grant_ref = Column(String, nullable=True)               # payment/order id -> idempotent grants
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.utcnow())
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.utcnow(), onupdate=lambda: datetime.utcnow())

    __table_args__ = (
        CheckConstraint("available_tokens >= 0", name="ck_hh_ent_available_nonneg"),
        CheckConstraint("available_tokens + total_claimed_tokens <= total_allocated_tokens",
                        name="ck_hh_ent_never_exceeds_allocation"),
    )


class HappyHourSession(Base):
    """One row per activated token. Rows are never deleted: they are the session history."""
    __tablename__ = "happy_hour_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    entitlement_id = Column(Integer, ForeignKey("happy_hour_entitlements.id", ondelete="SET NULL"), nullable=True)
    subscription_id = Column(String, nullable=True)
    status = Column(String, nullable=False, default="ACTIVE")  # ACTIVE | EXPIRED | LIMIT_REACHED | INVALIDATED
    started_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False)              # started_at + exactly 3600s
    max_video_generations = Column(Integer, nullable=False)    # snapshot of the admin setting at activation
    successful_video_generations = Column(Integer, nullable=False, default=0)
    in_flight_generations = Column(Integer, nullable=False, default=0)  # reserved slots of running generations
    idempotency_key = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.utcnow())
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.utcnow(), onupdate=lambda: datetime.utcnow())

    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_happy_hour_idempotency"),
        # DB-level guarantee: at most ONE active session per user, even under concurrent activations.
        Index("uq_happy_hour_one_active", "user_id", unique=True,
              sqlite_where=text("status = 'ACTIVE'"),
              postgresql_where=text("status = 'ACTIVE'")),
        Index("ix_happy_hour_sessions_user_started", "user_id", "started_at"),
        Index("ix_happy_hour_sessions_status_expires", "status", "expires_at"),
        CheckConstraint("successful_video_generations + in_flight_generations <= max_video_generations",
                        name="ck_hh_session_within_limit"),
    )


class AppSetting(Base):
    """Tiny key/value store for admin-editable settings (no code change / deploy needed)."""
    __tablename__ = "app_settings"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.utcnow(), onupdate=lambda: datetime.utcnow())
    updated_by_user_id = Column(Integer, nullable=True)


# =====================================================================
# NOTIFICATIONS (in-app inbox for the website + Android, FCM device tokens)
# =====================================================================
class Notification(Base):
    """Inbox row. Source of truth for the website bell and the Android in-app list; push is a best-effort extra."""
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    type = Column(String, nullable=False)          # HAPPY_HOUR_TOKEN_AVAILABLE | _STARTED | _ENDING_SOON | _ENDED | _BROADCAST
    title = Column(String, nullable=False)
    body = Column(String, nullable=False)
    data = Column(Text, nullable=True)             # JSON text (route, session_id, expires_at, ...)
    dedupe_key = Column(String, nullable=False)    # the same event can never create a second notice for a user
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.utcnow())
    read_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "dedupe_key", name="uq_notification_dedupe"),
        Index("ix_notifications_user_created", "user_id", "created_at"),
        Index("ix_notifications_user_read", "user_id", "read_at"),
    )


class DeviceToken(Base):
    """FCM registration token of an Android install, owned by whoever is logged in on it."""
    __tablename__ = "device_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token = Column(String, nullable=False, unique=True)
    platform = Column(String, nullable=False, default="android")
    app_version = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.utcnow())
    last_seen_at = Column(DateTime, nullable=False, default=lambda: datetime.utcnow())
    disabled_at = Column(DateTime, nullable=True)  # set when FCM reports the token as dead


class NotificationBroadcast(Base):
    """Admin announcements (e.g. a 'Happy Hour is live' message) and their reach."""
    __tablename__ = "notification_broadcasts"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    body = Column(String, nullable=False)
    audience = Column(String, nullable=False)      # eligible | all_subscribers
    created_by_user_id = Column(Integer, nullable=True)
    recipient_count = Column(Integer, nullable=False, default=0)
    device_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.utcnow())
