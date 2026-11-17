from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import User, HappyHourSession
import happy_hour as hh
import notifications as nt

# HappyHourError is turned into {"detail": "...", "code": "..."} by the handler registered in main.py.
router = APIRouter(prefix="/happy-hour", tags=["Happy Hour"])


@router.get("/status")
def happy_hour_status(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Tokens, eligibility, whether activation is possible, live session and remaining video allowance."""
    return hh.build_status(db, current_user)


@router.post("/activate")
def activate_happy_hour(
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key", max_length=128),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Spend one token and start a 60-minute session. Send a fresh `Idempotency-Key` per user action;
    retries with the same key return the same session and never spend a second token.
    """
    session, replayed = hh.activate_token(db, current_user, idempotency_key)
    if not replayed:
        nt.notify_session_started(db, session)   # inbox + Android push; deduped, never raises
    status = hh.build_status(db, current_user)
    status["idempotent_replay"] = replayed
    return status


@router.get("/session")
def happy_hour_session(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """The live session only (server expiry, remaining seconds, remaining video allowance)."""
    return hh.build_session_info(db, current_user)


@router.get("/history")
def happy_hour_history(limit: int = Query(50, ge=1, le=200),
                       current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    now = hh.utcnow()
    hh.get_active_session(db, current_user, now)  # expire a stale session so history is accurate
    rows = (
        db.query(HappyHourSession)
        .filter(HappyHourSession.user_id == current_user.id)
        .order_by(HappyHourSession.started_at.desc())
        .limit(limit)
        .all()
    )
    return [hh.serialize_session(r, now) for r in rows]
