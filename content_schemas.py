"""Pydantic request models shared by the popup / offer-banner / pricing admin APIs (validation + sanitising)."""
from datetime import datetime, timezone
from typing import Optional, List

from pydantic import BaseModel, Field, field_validator, model_validator, StrictInt

import content_validation as cv


def _naive_utc(v: Optional[datetime]) -> Optional[datetime]:
    if v is None:
        return None
    if v.tzinfo is not None:
        v = v.astimezone(timezone.utc).replace(tzinfo=None)
    return v


class _PromoFields(BaseModel):
    """Fields common to popups and the pricing-page offer banner."""
    title: str
    description: Optional[str] = None
    image_url: Optional[str] = None
    button_text: Optional[str] = None
    button_link: Optional[str] = None
    coupon_code: Optional[str] = None
    start_at: Optional[datetime] = None      # null = effective immediately
    end_at: Optional[datetime] = None        # null = never expires

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        v = cv.clean_text(v, max_len=120)
        if not v:
            raise ValueError("title is required")
        return v

    @field_validator("description")
    @classmethod
    def _desc(cls, v):
        return cv.clean_text(v, max_len=1000, multiline=True)

    @field_validator("button_text")
    @classmethod
    def _btn(cls, v):
        return cv.clean_text(v, max_len=40)

    @field_validator("button_link")
    @classmethod
    def _link(cls, v):
        return cv.clean_link(v)

    @field_validator("image_url")
    @classmethod
    def _img(cls, v):
        return cv.clean_image_url(v)

    @field_validator("coupon_code")
    @classmethod
    def _coupon(cls, v):
        return cv.clean_coupon(v)

    @field_validator("start_at", "end_at")
    @classmethod
    def _dt(cls, v):
        return _naive_utc(v)

    @model_validator(mode="after")
    def _window(self):
        if self.start_at and self.end_at and self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        if self.button_link and not self.button_text:
            raise ValueError("button_text is required when button_link is set")
        return self


class PopupIn(_PromoFields):
    target_audience: str = Field("ALL", pattern="^(ALL|FREE|PAID)$")
    status: str = Field("INACTIVE", pattern="^(ACTIVE|INACTIVE)$")
    priority: StrictInt = Field(0, ge=0, le=1000)


class BannerIn(_PromoFields):
    is_active: bool = False


class StatusIn(BaseModel):
    status: str = Field(..., pattern="^(ACTIVE|INACTIVE|ARCHIVED)$")


class BannerStatusIn(BaseModel):
    is_active: bool


def _clean_features(v: Optional[List[str]]) -> Optional[List[str]]:
    if v is None:
        return None
    if len(v) > 30:
        raise ValueError("at most 30 features")
    out = []
    for item in v:
        c = cv.clean_text(item, max_len=140)
        if c:
            out.append(c)
    return out


class PlanFields(BaseModel):
    """Editable display fields. `price_minor`, when given, triggers a payment-gateway price sync."""
    name: Optional[str] = None
    description: Optional[str] = None
    credits: Optional[StrictInt] = Field(None, ge=0, le=10_000_000)
    features: Optional[List[str]] = None
    is_most_popular: Optional[bool] = None
    badge_text: Optional[str] = None
    display_order: Optional[StrictInt] = Field(None, ge=0, le=10_000)
    is_visible: Optional[bool] = None
    is_active: Optional[bool] = None
    price_minor: Optional[StrictInt] = Field(None, gt=0, le=100_000_000)     # smallest currency unit (cents)
    currency: Optional[str] = Field(None, pattern="^[A-Za-z]{3}$")
    tax_category: Optional[str] = Field(None, pattern="^(digital_products|saas|e_book|edtech)$")

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        if v is None:
            return v
        v = cv.clean_text(v, max_len=60)
        if not v:
            raise ValueError("name cannot be empty")
        return v

    @field_validator("description")
    @classmethod
    def _desc(cls, v):
        return cv.clean_text(v, max_len=400, multiline=True) if v is not None else None

    @field_validator("badge_text")
    @classmethod
    def _badge(cls, v):
        return cv.clean_text(v, max_len=30) if v is not None else None

    @field_validator("features")
    @classmethod
    def _features(cls, v):
        return _clean_features(v)


class PlanCreate(PlanFields):
    plan_key: str = Field(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9 _-]{1,39}$")
    kind: str = Field(..., pattern="^(workspace|api|one_time)$")
    billing_interval: str = Field(..., pattern="^(monthly|yearly|one_time)$")
    name: str = Field(...)
    credits: StrictInt = Field(0, ge=0, le=10_000_000)

    @model_validator(mode="after")
    def _consistent(self):
        if (self.kind == "one_time") != (self.billing_interval == "one_time"):
            raise ValueError("one_time plans must use billing_interval 'one_time' and vice versa")
        return self


class PlanOrderItem(BaseModel):
    id: int
    display_order: StrictInt = Field(..., ge=0, le=10_000)


class PlanReorder(BaseModel):
    items: List[PlanOrderItem] = Field(..., min_length=1, max_length=200)


class PlanStatusIn(BaseModel):
    is_active: Optional[bool] = None
    is_visible: Optional[bool] = None
