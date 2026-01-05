from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

# Import your existing modules
from database import get_db
from models import User
from schemas import UserResponse 
from auth import get_current_user

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

# --- NEW ROUTE: DELETE USER ---
@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db), 
    admin: User = Depends(get_current_admin)
):
    """
    Deletes a user by ID. 
    Prevents admin from deleting themselves.
    """
    # 1. Find user
    user_to_delete = db.query(User).filter(User.id == user_id).first()
    if not user_to_delete:
        raise HTTPException(status_code=404, detail="User not found")

    # 2. Safety Check
    if user_to_delete.id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own admin account.")

    # 3. Delete
    db.delete(user_to_delete)
    db.commit()
    
    return {"status": "success", "message": f"User {user_to_delete.email} deleted successfully"}