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

# --- NEW: Schema for Admin Updates ---
class UserUpdate(BaseModel):
    credits: int
    plan: str

class Token(BaseModel):
    access_token: str
    token_type: str

class UserResponse(BaseModel):
    id: int               
    email: EmailStr       
    full_name: Optional[str] = None
    credits: int
    plan: str
    profile_pic: Optional[str] = None
    
    is_admin: bool = False 
    subscription_status: Optional[str] = "inactive"
    subscription_id: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

class OTPVerify(BaseModel):
    email: str
    otp: str