from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

# Import your database and models
from database import get_db
from models import User
from schemas import UserResponse, UserUpdate 
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