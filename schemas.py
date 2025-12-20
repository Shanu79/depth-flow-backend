# schemas.py
from typing import Optional
from pydantic import BaseModel, EmailStr

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class UserCreate(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    plan: str = "free"

class Token(BaseModel):
    access_token: str
    token_type: str

class UserResponse(BaseModel):
    email: str
    credits: int
    plan: str
    
class UserResponse(BaseModel):
    id: int                # Added ID (useful for React keys)
    email: str
    full_name: Optional[str] = None
    credits: int
    plan: str
    profile_pic: Optional[str] = None
    
    # Critical for Admin Panel
    is_admin: bool = False 
    subscription_status: Optional[str] = "inactive"

    # Critical for SQLAlchemy
    class Config:
        orm_mode = True