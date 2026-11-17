from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import happy_hour as hh
import notifications as nt
from auth import get_current_user
from database import get_db
from models import User

router = APIRouter(prefix="/notifications", tags=["Notifications"])


class DeviceRegistration(BaseModel):
    token: str = Field(min_length=20, max_length=4096)          # FCM registration token
    platform: str = Field(default="android", pattern="^android$")
    app_version: Optional[str] = Field(default=None, max_length=50)


class DeviceUnregister(BaseModel):
    token: str = Field(min_length=20, max_length=4096)


@router.post("/devices")
def register_device(body: DeviceRegistration, current_user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    """Android: call after login and whenever Firebase issues a new token (onNewToken)."""
    nt.register_device(db, current_user.id, body.token, body.platform, body.app_version)
    return {"status": "registered"}


@router.post("/devices/unregister")
def unregister_device(body: DeviceUnregister, current_user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    """Android: call on logout so the next user of the phone does not receive this account's pushes."""
    return {"status": "unregistered" if nt.unregister_device(db, current_user.id, body.token) else "not_found"}


@router.get("")
def list_notifications(
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, ge=0, description="only items newer than this id (incremental polling)"),
    before_id: Optional[int] = Query(None, ge=1, description="only items older than this id (paging back)"),
    unread_only: bool = False,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items = nt.list_notifications(db, current_user.id, limit, after_id, before_id, unread_only)
    return {
        "server_time": hh.iso(hh.utcnow()),
        "unread_count": nt.unread_count(db, current_user.id),
        "items": [nt.serialize_notification(n) for n in items],
    }


@router.get("/unread-count")
def unread_count(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Cheap call for the website bell badge: poll every 30-60 s and on tab focus."""
    return {"server_time": hh.iso(hh.utcnow()), "unread_count": nt.unread_count(db, current_user.id)}


@router.post("/read-all")
def read_all(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return {"status": "success", "marked": nt.mark_all_read(db, current_user.id), "unread_count": 0}


@router.post("/{notification_id}/read")
def read_one(notification_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not nt.mark_read(db, current_user.id, notification_id):
        raise HTTPException(status_code=404, detail="Notification not found.")
    return {"status": "success", "unread_count": nt.unread_count(db, current_user.id)}
