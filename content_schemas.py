"""Pydantic request models shared by the popup / offer-banner / pricing admin APIs (validation + sanitising)."""
import re
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
        if self.button_link and not self.button_text and getattr(self, "display_mode", "designed") != "image":
            raise ValueError("button_text is required when button_link is set")
        return self


class _StyleFields(BaseModel):
    """Look-and-feel fields shared by popups and the offer banner (all optional, app defaults otherwise)."""
    button_color: Optional[str] = None
    text_color: Optional[str] = None
    bg_color: Optional[str] = None
    accent_color: Optional[str] = None
    header_text: Optional[str] = None        # popup: strip across the top; banner: small label above the title
    show_countdown: bool = False             # counts down to end_at
    bg_color2: Optional[str] = None          # second background colour (gradient); null = flat background
    theme: str = Field("none", pattern="^(none|snow|lights|confetti|sparkles|rain)$")   # animated decoration
    emoji: Optional[str] = None              # decorative emoji (also what falls for theme "rain")
    font_style: str = Field("modern", pattern="^(modern|elegant|bold)$")
    bg_image_url: Optional[str] = None       # full-bleed background picture (tinted by the colours)
    display_mode: str = Field("designed", pattern="^(designed|image)$")   # image = a finished creative, no text layout

    @field_validator("bg_image_url")
    @classmethod
    def _bgimg(cls, v):
        return cv.clean_image_url(v)

    @field_validator("emoji")
    @classmethod
    def _emoji(cls, v):
        return cv.clean_text(v, max_len=8)

    @field_validator("button_color", "text_color", "bg_color", "bg_color2", "accent_color")
    @classmethod
    def _color(cls, v):
        if v is None or not str(v).strip():
            return None
        v = str(v).strip()
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            raise ValueError("colours must be hex like #7c3aed")
        return v.lower()

    @field_validator("header_text")
    @classmethod
    def _header(cls, v):
        return cv.clean_text(v, max_len=80)

    @model_validator(mode="after")
    def _countdown(self):
        if self.display_mode == "image":
            if not getattr(self, "image_url", None):
                raise ValueError("image_url is required when display_mode is 'image'")
            self.show_countdown = False
            return self
        if self.show_countdown and not getattr(self, "end_at", None):
            raise ValueError("an end date is required to show a countdown")
        return self


class PopupIn(_PromoFields, _StyleFields):
    target_audience: str = Field("ALL", pattern="^(ALL|FREE|PAID)$")
    status: str = Field("INACTIVE", pattern="^(ACTIVE|INACTIVE)$")
    priority: StrictInt = Field(0, ge=0, le=1000)
    footnote: Optional[str] = None
    layout: str = Field("split", pattern="^(split|stack)$")

    @field_validator("footnote")
    @classmethod
    def _foot(cls, v):
        return cv.clean_text(v, max_len=160)


class BannerIn(_PromoFields, _StyleFields):
    is_active: bool = False
    badge_text: Optional[str] = None         # big highlight such as "50% OFF"
    mobile_image_url: Optional[str] = None   # image-mode only: the phone-sized creative

    @field_validator("mobile_image_url")
    @classmethod
    def _mobile_img(cls, v):
        return cv.clean_image_url(v)

    @field_validator("badge_text")
    @classmethod
    def _badge(cls, v):
        return cv.clean_text(v, max_len=24)


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


# --- reusable admin templates ("save this design as a template") -------------------------------------------
_TEMPLATE_KEYS = {
    "popup": ["title", "description", "image_url", "button_text", "button_link", "coupon_code", "button_color",
              "text_color", "bg_color", "bg_color2", "accent_color", "header_text", "show_countdown", "theme", "emoji",
              "font_style", "bg_image_url", "footnote", "layout", "display_mode"],
    "banner": ["title", "description", "image_url", "button_text", "button_link", "coupon_code", "button_color",
               "text_color", "bg_color", "bg_color2", "accent_color", "header_text", "show_countdown", "theme", "emoji",
               "font_style", "bg_image_url", "badge_text", "display_mode", "mobile_image_url"],
}


class TemplateIn(BaseModel):
    kind: str = Field(..., pattern="^(popup|banner)$")
    name: str
    emoji: Optional[str] = None
    data: dict
    end_in_days: Optional[StrictInt] = Field(None, ge=1, le=365)    # a countdown template re-applies "ends in N days"

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        v = cv.clean_text(v, max_len=40)
        if not v:
            raise ValueError("name is required")
        return v

    @field_validator("emoji")
    @classmethod
    def _emoji(cls, v):
        return cv.clean_text(v, max_len=8)

    @model_validator(mode="after")
    def _validate_data(self):
        """Run the saved fields through the same validation as a real popup / banner and keep only known keys."""
        allowed = _TEMPLATE_KEYS[self.kind]
        raw = {k: v for k, v in self.data.items() if k in allowed}
        probe = dict(raw)
        probe.setdefault("title", self.name)
        if probe.get("show_countdown"):
            probe["end_at"] = "2099-01-01T00:00:00Z"               # only needed to satisfy the countdown rule
        model = PopupIn if self.kind == "popup" else BannerIn
        checked = model(**probe).model_dump()
        self.data = {k: checked[k] for k in allowed if k in raw}
        return self


# --- "What's New" modal releases ---------------------------------------------------------------------------
class WhatsNewFeature(BaseModel):
    icon: str
    title: str
    description: Optional[str] = None

    @field_validator("icon")
    @classmethod
    def _icon(cls, v):
        if v not in cv.WHATS_NEW_ICONS:
            raise ValueError("unknown icon")
        return v

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        v = cv.clean_text(v, max_len=80)
        if not v:
            raise ValueError("feature title is required")
        return v

    @field_validator("description")
    @classmethod
    def _desc(cls, v):
        return cv.clean_text(v, max_len=240)


class WhatsNewIn(BaseModel):
    tab_label: str
    badge_text: Optional[str] = None
    title: str
    subtitle: Optional[str] = None
    features: List[WhatsNewFeature] = Field(default_factory=list, max_length=12)
    button1_text: Optional[str] = None
    button1_link: Optional[str] = None
    button1_style: str = Field("light", pattern="^(google_play|light|dark)$")
    button2_text: Optional[str] = None
    button2_link: Optional[str] = None
    auto_show: bool = False
    status: str = Field("INACTIVE", pattern="^(ACTIVE|INACTIVE)$")
    display_order: StrictInt = Field(0, ge=0, le=1000)

    @field_validator("tab_label")
    @classmethod
    def _tab(cls, v):
        v = cv.clean_text(v, max_len=30)
        if not v:
            raise ValueError("tab_label is required")
        return v

    @field_validator("title")
    @classmethod
    def _t(cls, v):
        v = cv.clean_text(v, max_len=120)
        if not v:
            raise ValueError("title is required")
        return v

    @field_validator("badge_text")
    @classmethod
    def _badge(cls, v):
        return cv.clean_text(v, max_len=40)

    @field_validator("subtitle")
    @classmethod
    def _sub(cls, v):
        return cv.clean_text(v, max_len=400, multiline=True)

    @field_validator("button1_text", "button2_text")
    @classmethod
    def _btn(cls, v):
        return cv.clean_text(v, max_len=40)

    @field_validator("button1_link", "button2_link")
    @classmethod
    def _link(cls, v):
        return cv.clean_link(v)

    @model_validator(mode="after")
    def _buttons(self):
        if self.button1_link and not self.button1_text:
            raise ValueError("button1_text is required when button1_link is set")
        if self.button2_link and not self.button2_text:
            raise ValueError("button2_text is required when button2_link is set")
        if self.button1_style == "google_play" and self.button1_text and not self.button1_link:
            raise ValueError("a Google Play button needs a link")
        return self
