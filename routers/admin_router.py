from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

# Import your existing modules
from database import get_db
from models import User
from schemas import UserResponse # <--- Ensures we don't send passwords back
from auth import get_current_user # Reuse your existing login logic

router = APIRouter(prefix="/admin", tags=["Admin Dashboard"])

# --- SECURITY DEPENDENCY ---
# This function runs before every admin request.
# If the user is not an admin, it throws a 403 Forbidden error.
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
    limit: int = 100, 
    db: Session = Depends(get_db), 
    admin: User = Depends(get_current_admin) # <--- Enforce Admin Check
):
    """
    Fetches a list of users.
    Only accessible by users with is_admin=True.
    """
    users = db.query(User).offset(skip).limit(limit).all()
    return users