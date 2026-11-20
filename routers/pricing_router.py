import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import content_service as cs
import pricing
import pricing_gateway
from content_schemas import (PlanFields, PlanCreate, PlanReorder, PlanStatusIn, BannerIn, BannerStatusIn)
from database import get_db
from models import User, PricingPlan, OfferBanner
from routers.admin_router import get_current_admin

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Pricing"])

_PRICE_ONLY_FIELDS = {"price_minor", "currency", "tax_category"}


def get_gateway():
    """Dependency so tests can inject a fake gateway; production uses the real Dodo client."""
    return pricing_gateway.get_gateway()


# ---------------------------------------------------------------------------------------------------------
# PUBLIC
# ---------------------------------------------------------------------------------------------------------
@router.get("/pricing-plans")
def public_pricing_plans(
    response: Response,
    kind: Optional[str] = Query(None, pattern="^(workspace|api|one_time)$"),
    db: Session = Depends(get_db),
):
    """Active + visible plans with their current price. Gateway ids are never exposed; checkout is started with
    plan_key + billing_interval and the backend decides what is charged."""
    response.headers["Cache-Control"] = "public, max-age=10"
    return {"server_time": cs._iso(cs.utcnow()), "plans": pricing.list_public_plans(db, kind)}


@router.get("/offer-banner/active")
def public_offer_banner(response: Response, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "public, max-age=10"
    b = cs.active_banner(db)
    return {"server_time": cs._iso(cs.utcnow()), "banner": cs.serialize_banner_public(b) if b else None}


# ---------------------------------------------------------------------------------------------------------
# ADMIN: pricing plans
# ---------------------------------------------------------------------------------------------------------
def _plan_or_404(db: Session, plan_id: int) -> PricingPlan:
    plan = db.get(PricingPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan


def _sync_failed_response(db: Session, plan_id: int, err: pricing.PricingSyncError):
    db.expire_all()
    plan = db.get(PricingPlan, plan_id)
    return JSONResponse(status_code=502, content={
        "detail": f"Payment gateway sync failed; the previous price is still active. {err.message}",
        "code": "GATEWAY_SYNC_FAILED",
        "plan": pricing.admin_plan(db, plan),
    })


def _run_price_change(db, plan_id, price_minor, currency, admin_id, gateway, tax_category):
    """Returns None on success, or a JSONResponse (409/502) describing why the price was not published."""
    try:
        price_id = pricing.begin_price_change(db, plan_id, price_minor, currency, admin_id)
        if price_id is not None:
            pricing.sync_price(db, price_id, gateway, tax_category=tax_category)
    except pricing.PricingConflict as e:
        db.rollback()
        return JSONResponse(status_code=409, content={"detail": str(e) or "Pricing conflict", "code": "PRICING_CONFLICT"})
    except pricing.PricingSyncError as e:
        return _sync_failed_response(db, plan_id, e)
    return None


@router.get("/admin/pricing-plans")
def admin_list_plans(db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    plans = db.query(PricingPlan).order_by(PricingPlan.kind, PricingPlan.billing_interval,
                                           PricingPlan.display_order, PricingPlan.id).all()
    imported = len(plans) > 0
    return {"server_time": cs._iso(cs.utcnow()), "imported": imported,
            "plans": [pricing.admin_plan(db, p) for p in plans]}


@router.post("/admin/pricing-plans/import-from-dodo")
def admin_import_plans(db: Session = Depends(get_db), admin: User = Depends(get_current_admin),
                       gateway=Depends(get_gateway)):
    """One-time (idempotent) import of the existing plans with their real Dodo prices. Read-only on Dodo."""
    result = pricing.import_legacy_plans(db, gateway, admin.id)
    return result


@router.post("/admin/pricing-plans/reorder")
def admin_reorder_plans(body: PlanReorder, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    ids = [i.id for i in body.items]
    plans = {p.id: p for p in db.query(PricingPlan).filter(PricingPlan.id.in_(ids)).all()}
    if len(plans) != len(set(ids)):
        raise HTTPException(status_code=404, detail="One or more plans were not found")
    for item in body.items:
        plans[item.id].display_order = item.display_order
    db.commit()
    return {"status": "success"}


@router.post("/admin/pricing-plans", status_code=201)
def admin_create_plan(body: PlanCreate, db: Session = Depends(get_db), admin: User = Depends(get_current_admin),
                      gateway=Depends(get_gateway)):
    if pricing.get_plan(db, body.plan_key, body.billing_interval) is not None:
        raise HTTPException(status_code=409, detail="A plan with this key and billing interval already exists")
    data = body.model_dump(exclude_unset=True)
    for k in _PRICE_ONLY_FIELDS:
        data.pop(k, None)
    data.update(plan_key=body.plan_key, kind=body.kind, billing_interval=body.billing_interval, name=body.name,
                currency=(body.currency or "USD").upper())
    try:
        plan = pricing.create_plan(db, data)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Could not create the plan (duplicate or Most Popular conflict)")
    if body.price_minor is not None:
        failure = _run_price_change(db, plan.id, body.price_minor, (body.currency or "USD"), admin.id, gateway,
                                    body.tax_category)
        if failure is not None:
            return failure
    db.refresh(plan)
    return pricing.admin_plan(db, plan)


@router.get("/admin/pricing-plans/{plan_id}")
def admin_get_plan(plan_id: int, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    return pricing.admin_plan(db, _plan_or_404(db, plan_id))


@router.put("/admin/pricing-plans/{plan_id}")
def admin_update_plan(plan_id: int, body: PlanFields, db: Session = Depends(get_db),
                      admin: User = Depends(get_current_admin), gateway=Depends(get_gateway)):
    """Edits display fields (instant, DB only) and, when `price_minor` is present, publishes a new price through
    the payment gateway. If the gateway fails the previous price stays active (HTTP 502) and can be retried."""
    _plan_or_404(db, plan_id)
    fields = body.model_dump(exclude_unset=True)
    price_minor = fields.pop("price_minor", None)
    currency = fields.pop("currency", None)
    tax_category = fields.pop("tax_category", None)
    if fields.get("name") is None:
        fields.pop("name", None)
    try:
        plan = pricing._lock_plan(db, plan_id)
        pricing.apply_plan_fields(db, plan, fields)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Another plan is already marked Most Popular in this group")

    if price_minor is not None:
        failure = _run_price_change(db, plan_id, price_minor, currency or plan.currency, admin.id, gateway,
                                    tax_category)
        if failure is not None:
            return failure
    db.expire_all()
    return pricing.admin_plan(db, db.get(PricingPlan, plan_id))


@router.patch("/admin/pricing-plans/{plan_id}/status")
def admin_set_plan_status(plan_id: int, body: PlanStatusIn, db: Session = Depends(get_db),
                          admin: User = Depends(get_current_admin)):
    plan = _plan_or_404(db, plan_id)
    if body.is_active is not None:
        plan.is_active = body.is_active
    if body.is_visible is not None:
        plan.is_visible = body.is_visible
    db.commit()
    db.refresh(plan)
    return pricing.admin_plan(db, plan)


@router.post("/admin/pricing-plans/{plan_id}/sync-payment-gateway")
def admin_sync_plan(plan_id: int, db: Session = Depends(get_db), admin: User = Depends(get_current_admin),
                    gateway=Depends(get_gateway)):
    """Retry a pending/failed price sync. Reuses the same Dodo product if an earlier attempt created it."""
    _plan_or_404(db, plan_id)
    pend = pricing.pending_price(db, plan_id)
    if pend is None:
        raise HTTPException(status_code=400, detail="There is no pending price to sync for this plan")
    failure = _run_price_change(db, plan_id, pend.price_minor, pend.currency, admin.id, gateway, None)
    if failure is not None:
        return failure
    db.expire_all()
    return pricing.admin_plan(db, db.get(PricingPlan, plan_id))


# ---------------------------------------------------------------------------------------------------------
# ADMIN: offer banner
# ---------------------------------------------------------------------------------------------------------
def _banner_or_404(db: Session, banner_id: int) -> OfferBanner:
    b = db.get(OfferBanner, banner_id)
    if b is None:
        raise HTTPException(status_code=404, detail="Banner not found")
    return b


@router.get("/admin/offer-banner")
def admin_list_banners(db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    now = cs.utcnow()
    rows = db.query(OfferBanner).order_by(OfferBanner.id.desc()).all()
    return {"server_time": cs._iso(now), "banners": [cs.serialize_banner_admin(b, now) for b in rows]}


@router.post("/admin/offer-banner", status_code=201)
def admin_create_banner(body: BannerIn, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    b = OfferBanner(**body.model_dump(), created_by_user_id=admin.id)
    db.add(b)
    db.commit()
    db.refresh(b)
    return cs.serialize_banner_admin(b)


@router.put("/admin/offer-banner/{banner_id}")
def admin_update_banner(banner_id: int, body: BannerIn, db: Session = Depends(get_db),
                        admin: User = Depends(get_current_admin)):
    b = _banner_or_404(db, banner_id)
    for k, v in body.model_dump().items():
        setattr(b, k, v)
    db.commit()
    db.refresh(b)
    return cs.serialize_banner_admin(b)


@router.patch("/admin/offer-banner/{banner_id}/status")
def admin_set_banner_status(banner_id: int, body: BannerStatusIn, db: Session = Depends(get_db),
                            admin: User = Depends(get_current_admin)):
    b = _banner_or_404(db, banner_id)
    b.is_active = body.is_active
    db.commit()
    db.refresh(b)
    return cs.serialize_banner_admin(b)


@router.delete("/admin/offer-banner/{banner_id}")
def admin_delete_banner(banner_id: int, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    b = _banner_or_404(db, banner_id)
    db.delete(b)
    db.commit()
    return {"status": "deleted", "id": banner_id}
