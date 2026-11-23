from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from sqlalchemy.orm import Session

import content_service as cs
import content_validation as cv
from auth import get_current_user
from content_schemas import PopupIn, StatusIn
from database import get_db
from models import User, Popup
from routers.admin_router import get_current_admin

router = APIRouter(tags=["Popups"])


# ---------------------------------------------------------------------------------------------------------
# PUBLIC (authenticated user): which popup, if any, should I show right now?
# ---------------------------------------------------------------------------------------------------------
@router.get("/popups/eligible")
def eligible_popups(
    limit: int = Query(1, ge=1, le=5),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Live popups this user may see and has NOT been shown today, best priority first.
    Show only the first one; call POST /popups/{id}/track when it is actually displayed."""
    now = cs.utcnow()
    popups = cs.eligible_popups(db, current_user, limit=limit, now=now)
    return {"server_time": cs._iso(now), "popups": [cs.serialize_popup_public(p) for p in popups]}


@router.post("/popups/{popup_id}/track")
def track_popup(
    popup_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Record that the popup was displayed. Safe to call twice: at most one row per user per popup per UTC day."""
    result = cs.track_display(db, current_user, popup_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Popup not found")
    return {"status": "recorded" if result else "already_tracked"}


# ---------------------------------------------------------------------------------------------------------
# ADMIN
# ---------------------------------------------------------------------------------------------------------
def _get_or_404(db: Session, popup_id: int) -> Popup:
    p = db.get(Popup, popup_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Popup not found")
    return p


@router.get("/admin/popups")
def admin_list_popups(
    status: Optional[str] = Query(None, pattern="^(ACTIVE|INACTIVE|ARCHIVED)$"),
    include_archived: bool = False,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    q = db.query(Popup)
    if status:
        q = q.filter(Popup.status == status)
    elif not include_archived:
        q = q.filter(Popup.status != "ARCHIVED")
    now = cs.utcnow()
    rows = q.order_by(Popup.priority.desc(), Popup.id.desc()).all()
    return {"server_time": cs._iso(now), "popups": [cs.serialize_popup_admin(db, p, now) for p in rows]}


@router.post("/admin/popups", status_code=201)
def admin_create_popup(body: PopupIn, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    p = Popup(**body.model_dump(), created_by_user_id=admin.id)
    db.add(p)
    db.commit()
    db.refresh(p)
    return cs.serialize_popup_admin(db, p)


@router.get("/admin/popups/{popup_id}")
def admin_get_popup(popup_id: int, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    return cs.serialize_popup_admin(db, _get_or_404(db, popup_id))


@router.put("/admin/popups/{popup_id}")
def admin_update_popup(popup_id: int, body: PopupIn, db: Session = Depends(get_db),
                       admin: User = Depends(get_current_admin)):
    p = _get_or_404(db, popup_id)
    if p.status == "ARCHIVED":
        raise HTTPException(status_code=409, detail="Archived popups cannot be edited. Restore it first.")
    for k, v in body.model_dump().items():
        setattr(p, k, v)
    db.commit()
    db.refresh(p)
    return cs.serialize_popup_admin(db, p)


@router.patch("/admin/popups/{popup_id}/status")
def admin_set_popup_status(popup_id: int, body: StatusIn, db: Session = Depends(get_db),
                           admin: User = Depends(get_current_admin)):
    p = _get_or_404(db, popup_id)
    p.status = body.status
    db.commit()
    db.refresh(p)
    return cs.serialize_popup_admin(db, p)


@router.delete("/admin/popups/{popup_id}")
def admin_archive_popup(popup_id: int, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    """Archive (soft delete): it stops showing immediately but its display history is kept."""
    p = _get_or_404(db, popup_id)
    p.status = "ARCHIVED"
    db.commit()
    return {"status": "archived", "id": popup_id}


# ---------------------------------------------------------------------------------------------------------
# ADMIN image upload (popups + offer banner)
# ---------------------------------------------------------------------------------------------------------
@router.post("/admin/uploads/image")
async def admin_upload_image(file: UploadFile = File(...), admin: User = Depends(get_current_admin)):
    data = await file.read(cv.MAX_IMAGE_BYTES + 1)       # never buffer more than the cap
    try:
        name = cv.save_image_bytes(data)
    except OverflowError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"url": cv.public_url_for(name)}
