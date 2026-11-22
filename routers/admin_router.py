from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, StrictInt
from sqlalchemy.orm import Session
from typing import List

# Import your database and models
from database import get_db
from models import User
from schemas import UserResponse, UserUpdate 
from auth import get_current_user
import happy_hour as hh
import notifications as nt

router = APIRouter(prefix="/admin", tags=["Admin Dashboard"])

# --- SECURITY DEPENDENCY ---
def get_current_admin(current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have admin privileges"
        )
    return current_user

# --- ROUTE: GET ALL USERS ---
@router.get("/users", response_model=List[UserResponse])
def get_all_users(
    skip: int = 0, 
    limit: int = 1000000, 
    db: Session = Depends(get_db), 
    admin: User = Depends(get_current_admin)
):
    users = db.query(User).offset(skip).limit(limit).all()
    return users

# --- ROUTE: DELETE USER ---
@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db), 
    admin: User = Depends(get_current_admin)
):
    """
    Deletes a user by ID. 
    Prevents admin from deleting themselves or other admins.
    """
    # 1. Find user
    user_to_delete = db.query(User).filter(User.id == user_id).first()
    if not user_to_delete:
        raise HTTPException(status_code=404, detail="User not found")

    # 2. Safety Check: Prevent deleting yourself
    if user_to_delete.id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own admin account.")

    # 3. Safety Check: Prevent deleting other admins
    if user_to_delete.is_admin:
        raise HTTPException(
            status_code=403, 
            detail="You cannot delete another admin account."
        )

    # 4. Delete (Only happens if checks pass)
    try:
        db.delete(user_to_delete)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to delete user: {str(e)}")
    
    return {"status": "success", "message": f"User {user_to_delete.email} deleted successfully"}

# --- ROUTE: UPDATE USER ---
@router.put("/users/{user_id}", response_model=UserResponse)
def update_user(
    user_id: int, 
    user_data: UserUpdate, 
    db: Session = Depends(get_db), 
    admin: User = Depends(get_current_admin)
):
    # 1. Fetch the user from the database
    user = db.query(User).filter(User.id == user_id).first()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="User not found"
        )

    # 2. Update the fields based on the schema
    user.credits = user_data.credits
    user.plan = user_data.plan

    # Optional: Auto-activate subscription if plan is changed to Paid
    if user_data.plan.lower() in ["basic", "pro"]:
        user.subscription_status = "active"
    elif user_data.plan.lower() == "free" and user.subscription_status == "active":
         # Optional: If you want to downgrade status when moving to Free
         user.subscription_status = "inactive"
    
    # 3. Commit changes
    try:
        db.commit()
        db.refresh(user) # Refresh to get updated data back
        return user
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail=f"Failed to update user: {str(e)}"
        )


# --- HAPPY HOUR SETTINGS (no code change / deploy needed) ---
class HappyHourSettings(BaseModel):
    # StrictInt: "10" / 10.5 / true are rejected; gt=0 -> positive integers only.
    max_videos_per_session: StrictInt = Field(gt=0, le=hh.MAX_VIDEOS_CEILING)


@router.get("/settings/happy-hour")
def get_happy_hour_settings(db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    return {
        "max_videos_per_session": hh.get_max_videos(db),
        "default": hh.DEFAULT_MAX_VIDEOS,
        "max_allowed": hh.MAX_VIDEOS_CEILING,
        "note": "Applies to Happy Hour sessions activated after the change; running sessions keep their snapshot.",
    }


@router.put("/settings/happy-hour")
def update_happy_hour_settings(body: HappyHourSettings, db: Session = Depends(get_db),
                               admin: User = Depends(get_current_admin)):
    try:
        value = hh.set_max_videos(db, body.max_videos_per_session, admin.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success", "max_videos_per_session": value}


# --- HAPPY HOUR BROADCAST (admin announcement -> every recipient's inbox + their Android devices) ---
class HappyHourBroadcast(BaseModel):
    title: str = Field(min_length=1, max_length=65)
    body: str = Field(min_length=1, max_length=240)
    # eligible        = subscribers who could activate a token right now (tokens left, not used this month,
    #                   no running session)
    # all_subscribers = every active Monthly/Yearly workspace subscriber
    audience: str = Field(default="eligible", pattern="^(eligible|all_subscribers)$")
    dry_run: bool = False       # true = only report how many people/devices would be reached


@router.post("/notifications/happy-hour-broadcast")
def send_happy_hour_broadcast(body: HappyHourBroadcast, db: Session = Depends(get_db),
                              admin: User = Depends(get_current_admin)):
    try:
        return nt.send_happy_hour_broadcast(db, admin.id, body.title.strip(), body.body.strip(),
                                            body.audience, body.dry_run)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/notifications/broadcasts")
def list_broadcasts(limit: int = 50, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    return nt.list_broadcasts(db, max(1, min(limit, 200)))
