# schemas.py
from typing import Optional
from pydantic import BaseModel, EmailStr, ConfigDict

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
    id: int               # Critical for React keys
    email: EmailStr       # Good practice to use EmailStr here too
    full_name: Optional[str] = None
    credits: int
    plan: str
    profile_pic: Optional[str] = None
    
    # Critical for Admin Panel
    is_admin: bool = False 
    subscription_status: Optional[str] = "inactive"

    # Pydantic V2 Configuration
    model_config = ConfigDict(from_attributes=True)