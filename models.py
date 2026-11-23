from sqlalchemy import (Column, Integer, String, Boolean, DateTime, Date, Text, ForeignKey,
                        UniqueConstraint, Index, CheckConstraint, text)
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
# ADMIN-MANAGED PRICING PLANS, POPUPS AND OFFER BANNER
# All datetimes are naive UTC (same convention as the rest of the backend).
# =====================================================================
def _utcnow():
    return datetime.utcnow()


class PricingPlan(Base):
    """One purchasable Dodo plan (e.g. Basic/monthly). `plan_key` is immutable: it is what users.plan,
    checkout and webhooks use. `name` is the editable display name. The price lives in `plan_prices`."""
    __tablename__ = "pricing_plans"

    id = Column(Integer, primary_key=True, index=True)
    plan_key = Column(String, nullable=False)                 # "Basic", "Pro API", "Credit Pack" ...
    kind = Column(String, nullable=False)                     # workspace | api | one_time
    billing_interval = Column(String, nullable=False)         # monthly | yearly | one_time
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    currency = Column(String, nullable=False, default="USD")
    credits = Column(Integer, nullable=False, default=0)      # per purchase (yearly = full-year total)
    features = Column(Text, nullable=True)                    # JSON list of strings
    is_most_popular = Column(Boolean, nullable=False, default=False)
    badge_text = Column(String, nullable=True)
    display_order = Column(Integer, nullable=False, default=0)
    is_visible = Column(Boolean, nullable=False, default=True)
    is_active = Column(Boolean, nullable=False, default=True)
    pricing_version = Column(Integer, nullable=False, default=0)   # version of the ACTIVE price
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("plan_key", "billing_interval", name="uq_pricing_plan_key_interval"),
        # At most one Most Popular plan per group shown together on the page.
        Index("uq_pricing_one_popular", "kind", "billing_interval", unique=True,
              sqlite_where=text("is_most_popular = 1"), postgresql_where=text("is_most_popular = true")),
    )


class PlanPrice(Base):
    """Immutable price versions of a plan. Exactly one ACTIVE row per plan is what checkout charges.
    RETIRED rows keep their Dodo product so existing subscribers' webhooks still resolve."""
    __tablename__ = "plan_prices"

    id = Column(Integer, primary_key=True, index=True)
    plan_id = Column(Integer, ForeignKey("pricing_plans.id", ondelete="CASCADE"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    price_minor = Column(Integer, nullable=False)             # smallest currency unit (cents)
    currency = Column(String, nullable=False, default="USD")
    credits = Column(Integer, nullable=False, default=0)      # snapshot at activation
    gateway_product_id = Column(String, nullable=True, unique=True)
    status = Column(String, nullable=False, default="PENDING")   # PENDING | ACTIVE | RETIRED | FAILED
    last_error = Column(Text, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    created_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    activated_at = Column(DateTime, nullable=True)
    retired_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("plan_id", "version", name="uq_plan_price_version"),
        Index("uq_plan_price_one_active", "plan_id", unique=True,
              sqlite_where=text("status = 'ACTIVE'"), postgresql_where=text("status = 'ACTIVE'")),
        Index("uq_plan_price_one_pending", "plan_id", unique=True,
              sqlite_where=text("status = 'PENDING'"), postgresql_where=text("status = 'PENDING'")),
    )


class Popup(Base):
    __tablename__ = "popups"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    image_url = Column(String, nullable=True)
    button_text = Column(String, nullable=True)
    button_link = Column(String, nullable=True)
    coupon_code = Column(String, nullable=True)
    button_color = Column(String, nullable=True)                       # "#RRGGBB"; null = app default
    text_color = Column(String, nullable=True)
    bg_color = Column(String, nullable=True)
    accent_color = Column(String, nullable=True)                       # title + header strip
    header_text = Column(String, nullable=True)                        # strip across the top of the card
    footnote = Column(String, nullable=True)                           # small print under the button
    layout = Column(String, nullable=False, default="split")          # split | stack
    show_countdown = Column(Boolean, nullable=False, default=False)   # counts down to end_at
    bg_color2 = Column(String, nullable=True)
    theme = Column(String, nullable=False, default="none")             # none|snow|lights|confetti|sparkles|rain
    emoji = Column(String, nullable=True)
    font_style = Column(String, nullable=False, default="modern")      # modern|elegant|bold
    bg_image_url = Column(String, nullable=True)
    display_mode = Column(String, nullable=False, default="designed")   # designed | image
    target_audience = Column(String, nullable=False, default="ALL")    # ALL | FREE | PAID
    start_at = Column(DateTime, nullable=True)                         # null = immediately
    end_at = Column(DateTime, nullable=True)                           # null = no expiry
    status = Column(String, nullable=False, default="INACTIVE")        # ACTIVE | INACTIVE | ARCHIVED
    priority = Column(Integer, nullable=False, default=0)              # higher shows first
    created_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (Index("ix_popups_status_window", "status", "start_at", "end_at"),)


class PopupDisplay(Base):
    """One row per (popup, user, UTC day): the DB itself enforces 'at most once per user per day'."""
    __tablename__ = "popup_displays"

    id = Column(Integer, primary_key=True, index=True)
    popup_id = Column(Integer, ForeignKey("popups.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    displayed_at = Column(DateTime, default=_utcnow, nullable=False)
    display_date = Column(Date, nullable=False)

    __table_args__ = (UniqueConstraint("popup_id", "user_id", "display_date", name="uq_popup_user_day"),)


class OfferBanner(Base):
    __tablename__ = "offer_banners"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    image_url = Column(String, nullable=True)
    button_text = Column(String, nullable=True)
    button_link = Column(String, nullable=True)
    coupon_code = Column(String, nullable=True)
    button_color = Column(String, nullable=True)
    text_color = Column(String, nullable=True)
    bg_color = Column(String, nullable=True)
    accent_color = Column(String, nullable=True)
    header_text = Column(String, nullable=True)
    badge_text = Column(String, nullable=True)
    show_countdown = Column(Boolean, nullable=False, default=False)
    bg_color2 = Column(String, nullable=True)
    theme = Column(String, nullable=False, default="none")             # none|snow|lights|confetti|sparkles|rain
    emoji = Column(String, nullable=True)
    font_style = Column(String, nullable=False, default="modern")      # modern|elegant|bold
    bg_image_url = Column(String, nullable=True)
    display_mode = Column(String, nullable=False, default="designed")   # designed | image
    mobile_image_url = Column(String, nullable=True)
    start_at = Column(DateTime, nullable=True)
    end_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, nullable=False, default=False)
    created_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class PromoTemplate(Base):
    """A design an admin saved to reuse for popups / banners (festival looks etc.). `data` is validated JSON."""
    __tablename__ = "promo_templates"

    id = Column(Integer, primary_key=True, index=True)
    kind = Column(String, nullable=False, index=True)      # popup | banner
    name = Column(String, nullable=False)
    emoji = Column(String, nullable=True)
    data = Column(Text, nullable=False)                     # JSON of the design/content fields
    end_in_days = Column(Integer, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=_utcnow)


class WhatsNewRelease(Base):
    """One tab of the "What's New" modal. `features` is a JSON list of {icon, title, description}."""
    __tablename__ = "whats_new_releases"

    id = Column(Integer, primary_key=True, index=True)
    tab_label = Column(String, nullable=False)
    badge_text = Column(String, nullable=True)
    title = Column(String, nullable=False)
    subtitle = Column(Text, nullable=True)
    features = Column(Text, nullable=False, default="[]")
    button1_text = Column(String, nullable=True)
    button1_link = Column(String, nullable=True)
    button1_style = Column(String, nullable=False, default="light")    # google_play | light | dark
    button2_text = Column(String, nullable=True)
    button2_link = Column(String, nullable=True)                       # empty = just closes the modal
    auto_show = Column(Boolean, nullable=False, default=False)         # open once for users who have not seen it
    status = Column(String, nullable=False, default="INACTIVE")        # ACTIVE | INACTIVE | ARCHIVED
    display_order = Column(Integer, nullable=False, default=0)         # tabs left -> right; ties: newest first
    created_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

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
