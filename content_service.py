"""
Popup eligibility / tracking and offer-banner selection. All eligibility decisions are made here on the server;
the frontend only renders what it is given.
"""
import logging
from datetime import datetime, date
from typing import List, Optional

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import User, Popup, PopupDisplay, OfferBanner

logger = logging.getLogger(__name__)

AUDIENCES = ("ALL", "FREE", "PAID")
POPUP_STATUSES = ("ACTIVE", "INACTIVE", "ARCHIVED")

_NON_SUBSCRIPTION_PLANS = {"", "free", "trial", "credit pack"}


def utcnow() -> datetime:
    return datetime.utcnow()


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.replace(microsecond=0).isoformat() + "Z" if dt else None


def _status_is_live(status: Optional[str]) -> bool:
    s = (status or "").strip().lower()
    return s == "active" or s.startswith("scheduled for cancellation")


def is_paid_subscriber(user: User) -> bool:
    """Paid = an active workspace OR API subscription. Credit Pack buyers / cancelled / free users are 'free'."""
    workspace = _status_is_live(user.subscription_status) and \
        (user.plan or "").strip().lower() not in _NON_SUBSCRIPTION_PLANS
    api = _status_is_live(user.api_subscription_status) and \
        (user.api_plan or "").strip().lower() not in _NON_SUBSCRIPTION_PLANS
    return workspace or api


def audience_matches(popup: Popup, user: User) -> bool:
    aud = popup.target_audience
    if aud == "ALL":
        return True
    paid = is_paid_subscriber(user)
    return paid if aud == "PAID" else (not paid)


def popup_state(p: Popup, now: datetime) -> str:
    """ARCHIVED | INACTIVE | SCHEDULED | EXPIRED | LIVE (admin list badge)."""
    if p.status == "ARCHIVED":
        return "ARCHIVED"
    if p.status != "ACTIVE":
        return "INACTIVE"
    if p.start_at and now < p.start_at:
        return "SCHEDULED"
    if p.end_at and now > p.end_at:
        return "EXPIRED"
    return "LIVE"


def serialize_popup_public(p: Popup) -> dict:
    return {"id": p.id, "title": p.title, "description": p.description or "", "image_url": p.image_url,
            "button_text": p.button_text, "button_link": p.button_link, "coupon_code": p.coupon_code,
            "button_color": p.button_color, "text_color": p.text_color, "bg_color": p.bg_color,
            "accent_color": p.accent_color, "header_text": p.header_text, "footnote": p.footnote,
            "layout": p.layout or "split",
            "bg_color2": p.bg_color2, "theme": p.theme or "none", "emoji": p.emoji,
            "font_style": p.font_style or "modern", "bg_image_url": p.bg_image_url,
            "display_mode": p.display_mode or "designed", "show_countdown": bool(p.show_countdown),
            "end_at": _iso(p.end_at), "priority": p.priority}


def serialize_popup_admin(db: Session, p: Popup, now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    views = db.query(PopupDisplay).filter(PopupDisplay.popup_id == p.id).count()
    d = serialize_popup_public(p)
    d.update({"target_audience": p.target_audience, "status": p.status, "state": popup_state(p, now),
              "start_at": _iso(p.start_at), "end_at": _iso(p.end_at), "total_displays": views,
              "created_at": _iso(p.created_at), "updated_at": _iso(p.updated_at)})
    return d


def _live_popups_query(db: Session, now: datetime):
    return (db.query(Popup)
            .filter(Popup.status == "ACTIVE",
                    or_(Popup.start_at.is_(None), Popup.start_at <= now),
                    or_(Popup.end_at.is_(None), Popup.end_at >= now)))


def eligible_popups(db: Session, user: User, limit: int = 1, now: Optional[datetime] = None) -> List[Popup]:
    """Live, audience-matched popups the user has NOT already been shown today (UTC date), by priority."""
    now = now or utcnow()
    today = now.date()
    seen = {pid for (pid,) in db.query(PopupDisplay.popup_id)
            .filter(PopupDisplay.user_id == user.id, PopupDisplay.display_date == today).all()}
    out: List[Popup] = []
    for p in _live_popups_query(db, now).order_by(Popup.priority.desc(), Popup.id.asc()).all():
        if p.id in seen or not audience_matches(p, user):
            continue
        out.append(p)
        if len(out) >= limit:
            break
    return out


def track_display(db: Session, user: User, popup_id: int, now: Optional[datetime] = None) -> Optional[bool]:
    """
    Record that `user` saw `popup_id` today. Returns True when a new row was written, False when it was already
    recorded today (idempotent), None when the popup is not currently displayable for this user.
    """
    now = now or utcnow()
    popup = _live_popups_query(db, now).filter(Popup.id == popup_id).first()
    if popup is None or not audience_matches(popup, user):
        return None
    today = now.date()
    exists = (db.query(PopupDisplay.id)
              .filter(PopupDisplay.popup_id == popup_id, PopupDisplay.user_id == user.id,
                      PopupDisplay.display_date == today).first())
    if exists:
        return False
    try:
        db.add(PopupDisplay(popup_id=popup_id, user_id=user.id, displayed_at=now, display_date=today))
        db.commit()
        return True
    except IntegrityError:           # a concurrent duplicate request won the race: the unique constraint held
        db.rollback()
        return False


# ----------------------------------------------------------------------------------------------------------
# Offer banner
# ----------------------------------------------------------------------------------------------------------
def banner_is_live(b: OfferBanner, now: datetime) -> bool:
    return bool(b.is_active) and (b.start_at is None or b.start_at <= now) and (b.end_at is None or b.end_at >= now)


def active_banner(db: Session, now: Optional[datetime] = None) -> Optional[OfferBanner]:
    now = now or utcnow()
    live = [b for b in db.query(OfferBanner).filter(OfferBanner.is_active == True).all()  # noqa: E712
            if banner_is_live(b, now)]
    if not live:
        return None
    live.sort(key=lambda b: (b.start_at or datetime.min, b.id), reverse=True)    # most recently started wins
    return live[0]


def serialize_banner_public(b: OfferBanner) -> dict:
    return {"id": b.id, "title": b.title, "description": b.description or "", "image_url": b.image_url,
            "button_text": b.button_text, "button_link": b.button_link, "coupon_code": b.coupon_code,
            "button_color": b.button_color, "text_color": b.text_color, "bg_color": b.bg_color,
            "accent_color": b.accent_color, "header_text": b.header_text, "badge_text": b.badge_text,
            "show_countdown": bool(b.show_countdown),
            "bg_color2": b.bg_color2, "theme": b.theme or "none", "emoji": b.emoji,
            "font_style": b.font_style or "modern", "bg_image_url": b.bg_image_url,
            "display_mode": b.display_mode or "designed", "mobile_image_url": b.mobile_image_url, "end_at": _iso(b.end_at)}


def serialize_banner_admin(b: OfferBanner, now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    if not b.is_active:
        state = "DISABLED"
    elif b.start_at and now < b.start_at:
        state = "SCHEDULED"
    elif b.end_at and now > b.end_at:
        state = "EXPIRED"
    else:
        state = "LIVE"
    d = serialize_banner_public(b)
    d.update({"is_active": bool(b.is_active), "state": state, "start_at": _iso(b.start_at),
              "created_at": _iso(b.created_at), "updated_at": _iso(b.updated_at)})
    return d
