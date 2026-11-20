from sqlalchemy import (Column, Integer, String, Boolean, DateTime, Date, Text, ForeignKey,
                        UniqueConstraint, Index, text)
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
    start_at = Column(DateTime, nullable=True)
    end_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, nullable=False, default=False)
    created_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
