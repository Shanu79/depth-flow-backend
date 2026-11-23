import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

import whats_new as wn
from content_schemas import WhatsNewIn, StatusIn, PlanReorder
from database import get_db
from models import User, WhatsNewRelease
from routers.admin_router import get_current_admin

router = APIRouter(tags=["What's New"])


# --------------------------------------------------------------------------------------------------- public
@router.get("/whats-new")
def public_whats_new(db: Session = Depends(get_db)):
    """Live releases (the modal's tabs, in order) and the number that tells a browser a new announcement exists."""
    return {"releases": [wn.serialize_public(r) for r in wn.ordered_active(db)],
            "announce_version": wn.announce_version(db)}


# --------------------------------------------------------------------------------------------------- admin
def _get_or_404(db: Session, release_id: int) -> WhatsNewRelease:
    r = db.get(WhatsNewRelease, release_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Release not found")
    return r


def _apply(r: WhatsNewRelease, body: WhatsNewIn) -> None:
    data = body.model_dump()
    data["features"] = json.dumps(data["features"])
    for k, v in data.items():
        setattr(r, k, v)


@router.get("/admin/whats-new")
def admin_list(include_archived: bool = Query(False), db: Session = Depends(get_db),
               admin: User = Depends(get_current_admin)):
    q = db.query(WhatsNewRelease)
    if not include_archived:
        q = q.filter(WhatsNewRelease.status != "ARCHIVED")
    rows = q.order_by(WhatsNewRelease.display_order.asc(), WhatsNewRelease.id.desc()).all()
    return {"releases": [wn.serialize_admin(r) for r in rows],
            "has_any": db.query(WhatsNewRelease).count() > 0}


@router.post("/admin/whats-new/import-defaults", status_code=201)
def admin_import_defaults(db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    created = wn.import_defaults(db, admin.id)
    if not created:
        raise HTTPException(status_code=409, detail="There are already releases; nothing was imported.")
    return {"imported": len(created), "releases": [wn.serialize_admin(r) for r in created]}


@router.post("/admin/whats-new/reorder")
def admin_reorder(body: PlanReorder, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    for item in body.items:
        _get_or_404(db, item.id).display_order = item.display_order
    db.commit()
    return {"ok": True}


@router.post("/admin/whats-new", status_code=201)
def admin_create(body: WhatsNewIn, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    r = WhatsNewRelease(created_by_user_id=admin.id)
    _apply(r, body)
    db.add(r)
    db.commit()
    db.refresh(r)
    return wn.serialize_admin(r)


@router.get("/admin/whats-new/{release_id}")
def admin_get(release_id: int, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    return wn.serialize_admin(_get_or_404(db, release_id))


@router.put("/admin/whats-new/{release_id}")
def admin_update(release_id: int, body: WhatsNewIn, db: Session = Depends(get_db),
                 admin: User = Depends(get_current_admin)):
    r = _get_or_404(db, release_id)
    if r.status == "ARCHIVED":
        raise HTTPException(status_code=409, detail="Archived releases cannot be edited. Restore it first.")
    _apply(r, body)
    db.commit()
    db.refresh(r)
    return wn.serialize_admin(r)


@router.patch("/admin/whats-new/{release_id}/status")
def admin_status(release_id: int, body: StatusIn, db: Session = Depends(get_db),
                 admin: User = Depends(get_current_admin)):
    r = _get_or_404(db, release_id)
    r.status = body.status
    db.commit()
    db.refresh(r)
    return wn.serialize_admin(r)


@router.delete("/admin/whats-new/{release_id}")
def admin_archive(release_id: int, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    r = _get_or_404(db, release_id)
    r.status = "ARCHIVED"
    db.commit()
    return {"status": "archived", "id": release_id}
