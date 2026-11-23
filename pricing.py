"""
Pricing service: the single source of truth for purchasable plans (replaces the hardcoded PLAN_CONFIG etc.).

* `pricing_plans`  one row per purchasable plan (immutable `plan_key`, editable display fields, credits).
* `plan_prices`    immutable price versions; exactly one ACTIVE per plan, linked to a Dodo product.
  Old versions are RETIRED, never deleted, so existing subscribers' webhooks keep resolving.

Backward compatible: a plan with no row in the DB falls back to the LEGACY_* constants below, so deploying
this before running the one-time import changes nothing.
"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import PricingPlan, PlanPrice

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------------------------------------
# Legacy constants (previously defined in routers/payments_router.py). Used as the fallback and as the data
# for the one-time import. Do not edit prices here; they are managed in the Admin Panel from now on.
# ----------------------------------------------------------------------------------------------------------
LEGACY_PLAN_CONFIG = {
    "monthly": {
        "Basic": {"id": "pdt_0NUQxXnGKlkhrpGBAFMvy", "credits": 600},
        "Pro":   {"id": "pdt_0NUQxXJa0TR6vJJznsrt2", "credits": 1500},
        "Premium":   {"id": "pdt_0Nc5Ts7erauRnehAFJV6Q", "credits": 4000},
    },
    "yearly": {
        "Basic": {"id": "pdt_0NUQxWNFaRWTd8eoNkqoJ", "credits": 7200},
        "Pro":   {"id": "pdt_0NUQxVyW088PIAwIzObuC", "credits": 18000},
        "Premium": {"id": "pdt_0Nc5TfN5t1oLCfoeNN7AZ", "credits": 48000},
    }
}
LEGACY_API_PLAN_CONFIG = {
    "monthly": {
        "Starter API": {"id": "pdt_0Nbt4jmPtgBIPmJgF3qdh", "credits": 10000},
        "Growth API":  {"id": "pdt_0NbtLLrNFAwrGkvpH0lGQ", "credits": 30000},
        "Pro API":     {"id": "pdt_0Nbt4bIY0qlHH3u8SMyyD", "credits": 65000},
    },
    "yearly": {
        "Starter API": {"id": "dummy_api_starter_yearly", "credits": 120000},
        "Growth API":  {"id": "dummy_api_growth_yearly", "credits": 324000},
        "Pro API":     {"id": "dummy_api_pro_yearly", "credits": 720000},
    }
}
LEGACY_ONE_TIME_PLANS = {
    # "Trial": {"id": "pdt_0NUQxWtv1A7PDo75MPx9L", "credits": 120},
    "Credit Pack": {"id": "pdt_0NYCtBgPqWWzyF6yKrE98", "credits": 900},
}

INTERVALS = ("monthly", "yearly", "one_time")
KINDS = ("workspace", "api", "one_time")
SYNC_IN_PROGRESS_SECONDS = 120


def utcnow() -> datetime:
    return datetime.utcnow()


# ----------------------------------------------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------------------------------------------
class PlanUnavailable(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


class PricingConflict(Exception):
    """Another change/sync for the same plan is in progress (HTTP 409)."""


class PricingSyncError(Exception):
    """Payment gateway sync failed; the previously active price is still in force (HTTP 502)."""
    def __init__(self, message: str, price_id: Optional[int] = None):
        super().__init__(message)
        self.message, self.price_id = message, price_id


# ----------------------------------------------------------------------------------------------------------
# Lookups
# ----------------------------------------------------------------------------------------------------------
@dataclass
class ResolvedPlan:
    plan_key: str
    interval: str          # monthly | yearly | one_time
    kind: str              # workspace | api | one_time
    product_id: str
    credits: int

    @property
    def is_api(self) -> bool:
        return self.kind == "api"

    @property
    def is_one_time(self) -> bool:
        return self.kind == "one_time"


def get_plan(db: Session, plan_key: str, interval: str) -> Optional[PricingPlan]:
    return (db.query(PricingPlan)
            .filter(PricingPlan.plan_key == plan_key, PricingPlan.billing_interval == interval).first())


def active_price(db: Session, plan_id: int) -> Optional[PlanPrice]:
    return db.query(PlanPrice).filter(PlanPrice.plan_id == plan_id, PlanPrice.status == "ACTIVE").first()


def pending_price(db: Session, plan_id: int) -> Optional[PlanPrice]:
    return db.query(PlanPrice).filter(PlanPrice.plan_id == plan_id, PlanPrice.status == "PENDING").first()


def _legacy_entries():
    for interval, plans in LEGACY_PLAN_CONFIG.items():
        for key, d in plans.items():
            yield key, "workspace", interval, d
    for interval, plans in LEGACY_API_PLAN_CONFIG.items():
        for key, d in plans.items():
            yield key, "api", interval, d
    for key, d in LEGACY_ONE_TIME_PLANS.items():
        yield key, "one_time", "one_time", d


def resolve_for_checkout(db: Session, plan_name: str, billing_cycle: str) -> ResolvedPlan:
    """Backend-controlled plan lookup for checkout. The frontend never supplies a price or product id."""
    interval = billing_cycle
    plan = get_plan(db, plan_name, interval) if interval in INTERVALS else None
    if plan is not None:
        if not plan.is_active:
            raise PlanUnavailable(400, "This plan is not available for purchase.")
        price = active_price(db, plan.id)
        if price is None or not price.gateway_product_id:
            raise PlanUnavailable(400, "This plan is not available for purchase right now.")
        return ResolvedPlan(plan.plan_key, plan.billing_interval, plan.kind, price.gateway_product_id, price.credits)

    # --- legacy fallback (plan not imported into the DB yet) ---
    if billing_cycle == "one_time":
        d = LEGACY_ONE_TIME_PLANS.get(plan_name)
        if not d:
            raise PlanUnavailable(400, "Invalid one-time plan")
        return ResolvedPlan(plan_name, "one_time", "one_time", d["id"], d["credits"])
    if billing_cycle in LEGACY_PLAN_CONFIG and plan_name in LEGACY_PLAN_CONFIG[billing_cycle]:
        d = LEGACY_PLAN_CONFIG[billing_cycle][plan_name]
        return ResolvedPlan(plan_name, billing_cycle, "workspace", d["id"], d["credits"])
    if billing_cycle in LEGACY_API_PLAN_CONFIG and plan_name in LEGACY_API_PLAN_CONFIG[billing_cycle]:
        d = LEGACY_API_PLAN_CONFIG[billing_cycle][plan_name]
        return ResolvedPlan(plan_name, billing_cycle, "api", d["id"], d["credits"])
    raise PlanUnavailable(400, f"Invalid plan: {plan_name}")


def resolve_by_product_id(db: Session, product_id: Optional[str], api: bool) -> Optional[Tuple[str, str]]:
    """Dodo product id -> (plan_key, interval). Searches ALL price versions (retired ones too), then legacy."""
    if not product_id:
        return None
    pid = str(product_id).strip()
    kind = "api" if api else "workspace"
    row = (db.query(PricingPlan)
           .join(PlanPrice, PlanPrice.plan_id == PricingPlan.id)
           .filter(PlanPrice.gateway_product_id == pid, PricingPlan.kind == kind).first())
    if row:
        return row.plan_key, row.billing_interval
    config = LEGACY_API_PLAN_CONFIG if api else LEGACY_PLAN_CONFIG
    for interval, plans in config.items():
        for key, d in plans.items():
            if str(d["id"]).strip() == pid:
                return key, interval
    return None


def renewal_credits(db: Session, plan_key: Optional[str], interval: Optional[str], api: bool,
                    product_id: Optional[str] = None) -> int:
    """Credits for a renewal payment. Prefers the exact Dodo product the subscriber is on (so a later price/credit
    change on a newer product never alters an existing subscriber), then the plan's current credits, then legacy."""
    if product_id:
        price = db.query(PlanPrice).filter(PlanPrice.gateway_product_id == str(product_id).strip()).first()
        if price is not None:
            return int(price.credits or 0)
    if plan_key and interval:
        plan = get_plan(db, plan_key, interval)
        if plan is not None:
            price = active_price(db, plan.id)
            return int(price.credits if price else plan.credits or 0)
        config = LEGACY_API_PLAN_CONFIG if api else LEGACY_PLAN_CONFIG
        return int(config.get(interval, {}).get(plan_key, {}).get("credits", 0))
    return 0


def yearly_credits(db: Session, plan_key: str, api: bool) -> Optional[int]:
    """Full-year credits of a yearly plan, or None if the plan is unknown (used by the monthly credit cron)."""
    n = renewal_credits(db, plan_key, "yearly", api)
    return n if n > 0 else None


# ----------------------------------------------------------------------------------------------------------
# Serialisation
# ----------------------------------------------------------------------------------------------------------
def features_of(plan: PricingPlan) -> List[str]:
    try:
        v = json.loads(plan.features) if plan.features else []
        return [str(x) for x in v] if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.replace(microsecond=0).isoformat() + "Z" if dt else None


def public_plan(plan: PricingPlan, price: PlanPrice) -> dict:
    return {
        "plan_key": plan.plan_key,
        "kind": plan.kind,
        "billing_interval": plan.billing_interval,
        "name": plan.name,
        "description": plan.description or "",
        "currency": price.currency,
        "price_minor": price.price_minor,
        "price": round(price.price_minor / 100, 2),
        "credits": price.credits,
        "features": features_of(plan),
        "is_most_popular": bool(plan.is_most_popular),
        "badge_text": (plan.badge_text or "Most Popular") if plan.is_most_popular else None,
        "display_order": plan.display_order,
        "pricing_version": plan.pricing_version,
    }


def list_public_plans(db: Session, kind: Optional[str] = None) -> List[dict]:
    q = (db.query(PricingPlan)
         .filter(PricingPlan.is_active == True, PricingPlan.is_visible == True))  # noqa: E712
    if kind:
        q = q.filter(PricingPlan.kind == kind)
    out = []
    for plan in q.order_by(PricingPlan.display_order, PricingPlan.id).all():
        price = active_price(db, plan.id)
        if price is not None and price.gateway_product_id:
            out.append(public_plan(plan, price))
    return out


def admin_plan(db: Session, plan: PricingPlan) -> dict:
    act, pend = active_price(db, plan.id), pending_price(db, plan.id)
    if pend is not None:
        sync_status = "FAILED" if pend.last_error else "SYNCING"
    else:
        sync_status = "SYNCED" if act is not None else "NO_PRICE"
    return {
        "id": plan.id,
        "plan_key": plan.plan_key,
        "kind": plan.kind,
        "billing_interval": plan.billing_interval,
        "name": plan.name,
        "description": plan.description or "",
        "currency": plan.currency,
        "credits": plan.credits,
        "features": features_of(plan),
        "is_most_popular": bool(plan.is_most_popular),
        "badge_text": plan.badge_text,
        "display_order": plan.display_order,
        "is_visible": bool(plan.is_visible),
        "is_active": bool(plan.is_active),
        "pricing_version": plan.pricing_version,
        "sync_status": sync_status,
        "active_price": None if act is None else {
            "version": act.version, "price_minor": act.price_minor, "currency": act.currency,
            "credits": act.credits, "gateway_product_id": act.gateway_product_id,
            "activated_at": _iso(act.activated_at)},
        "proposed_price": None if pend is None else {
            "version": pend.version, "price_minor": pend.price_minor, "currency": pend.currency,
            "attempts": pend.attempts, "last_error": pend.last_error},
        "updated_at": _iso(plan.updated_at),
    }


# ----------------------------------------------------------------------------------------------------------
# Display-field edits (never touch the gateway, users or subscriptions)
# ----------------------------------------------------------------------------------------------------------
def _lock_plan(db: Session, plan_id: int) -> Optional[PricingPlan]:
    return db.query(PricingPlan).filter(PricingPlan.id == plan_id).with_for_update().first()


def apply_plan_fields(db: Session, plan: PricingPlan, fields: dict) -> None:
    """Apply already-validated display fields. Caller commits."""
    if "features" in fields and fields["features"] is not None:
        plan.features = json.dumps(fields["features"])
    for k in ("name", "description", "badge_text", "display_order", "is_visible", "is_active"):
        if k in fields:
            setattr(plan, k, fields[k])
    if "credits" in fields and fields["credits"] is not None:
        plan.credits = fields["credits"]
        act = active_price(db, plan.id)
        if act is not None:                     # applies to new purchases and renewals on the current product
            act.credits = fields["credits"]
    if fields.get("is_most_popular") is True and not plan.is_most_popular:
        (db.query(PricingPlan)
         .filter(PricingPlan.kind == plan.kind, PricingPlan.billing_interval == plan.billing_interval,
                 PricingPlan.id != plan.id, PricingPlan.is_most_popular == True)  # noqa: E712
         .update({"is_most_popular": False}, synchronize_session=False))
        db.flush()
        plan.is_most_popular = True
        if not plan.badge_text:
            plan.badge_text = "Most Popular"
    elif fields.get("is_most_popular") is False:
        plan.is_most_popular = False


def create_plan(db: Session, data: dict) -> PricingPlan:
    plan = PricingPlan(
        plan_key=data["plan_key"], kind=data["kind"], billing_interval=data["billing_interval"],
        name=data["name"], description=data.get("description"), currency=data.get("currency", "USD"),
        credits=data.get("credits", 0), features=json.dumps(data.get("features") or []),
        badge_text=data.get("badge_text"), display_order=data.get("display_order", 0),
        is_visible=data.get("is_visible", True), is_active=data.get("is_active", True),
        is_most_popular=False, pricing_version=0)
    db.add(plan)
    db.flush()
    if data.get("is_most_popular"):
        apply_plan_fields(db, plan, {"is_most_popular": True})
    return plan


# ----------------------------------------------------------------------------------------------------------
# Price change + gateway sync
# ----------------------------------------------------------------------------------------------------------
def begin_price_change(db: Session, plan_id: int, price_minor: int, currency: str,
                       admin_id: Optional[int]) -> Optional[int]:
    """
    Record the PROPOSED price as a PENDING version (no gateway call yet). Returns its id, or None when the
    requested price already is the active one. Concurrent callers are serialised by the plan row lock and the
    one-PENDING-per-plan unique index.
    """
    currency = currency.upper()
    try:
        plan = _lock_plan(db, plan_id)
        if plan is None:
            raise LookupError("plan not found")
        act, pend = active_price(db, plan.id), pending_price(db, plan.id)
        now = utcnow()
        if pend is not None:
            if pend.price_minor == price_minor and pend.currency == currency:
                db.commit()
                return pend.id                                   # retry of the same proposal
            in_progress = (pend.last_error is None and pend.attempts > 0 and pend.updated_at is not None
                           and now - pend.updated_at < timedelta(seconds=SYNC_IN_PROGRESS_SECONDS))
            if in_progress:
                raise PricingConflict("A price sync for this plan is already in progress. Try again shortly.")
            pend.status = "FAILED"                                # abandoned proposal (kept for the audit trail)
            pend.last_error = pend.last_error or "Superseded by a newer price."
            db.flush()
        if act is not None and act.price_minor == price_minor and act.currency == currency:
            db.commit()
            return None
        version = (db.query(func.max(PlanPrice.version)).filter(PlanPrice.plan_id == plan.id).scalar() or 0) + 1
        row = PlanPrice(plan_id=plan.id, version=version, price_minor=price_minor, currency=currency,
                        credits=plan.credits, status="PENDING", created_by_user_id=admin_id)
        db.add(row)
        db.flush()
        rid = row.id
        db.commit()
        return rid
    except IntegrityError:
        db.rollback()
        raise PricingConflict("Another price change for this plan is in progress.")
    except Exception:
        db.rollback()
        raise


def _record_failure(db: Session, price_id: int, message: str) -> None:
    db.rollback()
    row = db.get(PlanPrice, price_id)
    if row is not None and row.status == "PENDING":
        row.last_error = (message or "Unknown gateway error")[:500]
        db.commit()
    logger.error("Pricing sync failed for plan_price %s: %s", price_id, message)


def sync_price(db: Session, price_id: int, gateway, tax_category: Optional[str] = None) -> PlanPrice:
    """
    Create/verify the Dodo product for a PENDING price and, only if that succeeded, make it the ACTIVE price
    (retiring the previous one). On failure the previous active price stays untouched and the error is stored
    so the admin can retry. Retries reuse the same Dodo product (looked up by sync_key), never duplicating it.
    """
    from pricing_gateway import GatewayError

    row = db.get(PlanPrice, price_id)
    if row is None:
        raise LookupError("price not found")
    if row.status == "ACTIVE":
        return row
    if row.status != "PENDING":
        raise PricingConflict("This price proposal is no longer pending.")
    plan = db.get(PricingPlan, row.plan_id)

    row.attempts = (row.attempts or 0) + 1
    row.last_error = None
    row.updated_at = utcnow()
    db.commit()

    act = active_price(db, plan.id)
    template = act.gateway_product_id if act else (
        db.query(PlanPrice.gateway_product_id)
        .filter(PlanPrice.plan_id == plan.id, PlanPrice.gateway_product_id.isnot(None))
        .order_by(PlanPrice.version.desc()).limit(1).scalar())
    sync_key = f"{plan.plan_key}|{plan.billing_interval}|v{row.version}|{row.price_minor}|{row.currency}"
    spec = {
        "name": plan.name, "description": plan.description, "price_minor": row.price_minor,
        "currency": row.currency, "interval": plan.billing_interval, "template_product_id": template,
        "tax_category": tax_category,
        "metadata": {"plan_key": plan.plan_key, "interval": plan.billing_interval,
                     "version": row.version, "managed_by": "depthflow-admin"},
    }
    try:
        product_id = gateway.ensure_product(sync_key, spec)
    except GatewayError as e:
        _record_failure(db, price_id, str(e))
        raise PricingSyncError(str(e), price_id)
    except Exception as e:  # unexpected SDK/network error
        _record_failure(db, price_id, f"Unexpected gateway error: {e}")
        raise PricingSyncError(f"Unexpected gateway error: {e}", price_id)

    # --- publish: verified on the gateway, now switch the active price atomically ---
    try:
        plan = _lock_plan(db, plan.id)
        db.refresh(row)
        if row.status != "PENDING":
            db.rollback()
            raise PricingConflict("This price proposal is no longer pending.")
        now = utcnow()
        old = active_price(db, plan.id)
        if old is not None:
            old.status = "RETIRED"
            old.retired_at = now
            db.flush()
        row.gateway_product_id = product_id
        row.status = "ACTIVE"
        row.credits = plan.credits
        row.activated_at = now
        row.last_error = None
        plan.pricing_version = row.version
        plan.currency = row.currency
        db.commit()
    except PricingConflict:
        raise
    except Exception as e:
        _record_failure(db, price_id, f"Could not publish the new price: {e}")
        raise PricingSyncError(f"Could not publish the new price: {e}", price_id)
    return row


# ----------------------------------------------------------------------------------------------------------
# One-time import of the legacy plans (read-only calls to Dodo)
# ----------------------------------------------------------------------------------------------------------
def import_legacy_plans(db: Session, gateway, admin_id: Optional[int] = None) -> dict:
    """Idempotent. Creates a plan + ACTIVE version 1 for every legacy plan using the REAL price read from Dodo.
    Entries whose Dodo product can't be read are skipped (reported) and keep working through the legacy fallback."""
    from pricing_gateway import GatewayError

    imported, skipped, errors = [], [], []
    for order, (key, kind, interval, d) in enumerate(_legacy_entries(), start=1):
        plan = get_plan(db, key, interval)
        if plan is not None and active_price(db, plan.id) is not None:
            skipped.append(f"{key}/{interval}")
            continue
        pid = str(d["id"])
        if pid.startswith("dummy_"):
            errors.append(f"{key}/{interval}: placeholder product id, not imported")
            continue
        try:
            got = gateway.read_product(pid)
        except GatewayError as e:
            errors.append(f"{key}/{interval}: {e}")
            continue
        try:
            if plan is None:
                plan = PricingPlan(plan_key=key, kind=kind, billing_interval=interval, name=key,
                                   description="", currency=got["currency"], credits=d["credits"],
                                   features="[]", display_order=order, is_visible=True, is_active=True,
                                   is_most_popular=False, pricing_version=1)
                db.add(plan)
                db.flush()
            db.add(PlanPrice(plan_id=plan.id, version=1, price_minor=got["price_minor"], currency=got["currency"],
                             credits=d["credits"], gateway_product_id=pid, status="ACTIVE",
                             created_by_user_id=admin_id, activated_at=utcnow()))
            plan.pricing_version = 1
            db.commit()
            imported.append(f"{key}/{interval}")
        except Exception as e:
            db.rollback()
            errors.append(f"{key}/{interval}: {e}")
    return {"imported": imported, "skipped": skipped, "errors": errors}
