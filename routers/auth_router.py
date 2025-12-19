import os
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from fastapi_sso.sso.google import GoogleSSO
from pathlib import Path
from dotenv import load_dotenv

# --- NEW IMPORTS ---
from database import get_db
from models import User
from schemas import UserCreate, UserLogin  # <--- Import Schemas
from auth import (
    create_access_token, 
    get_current_user, 
    get_password_hash,  # <--- Import Hash Function
    verify_password     # <--- Import Verify Function
)

router = APIRouter(prefix="/auth", tags=["Authentication"])

# Force Load Env (Safety Check)
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

# Google Config
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
CALLBACK_URL = os.getenv("GOOGLE_REDIRECT_URI")

google_sso = GoogleSSO(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, CALLBACK_URL)


# ==========================================
# 1. EMAIL/PASSWORD REGISTRATION
# ==========================================
@router.post("/register")
def register(user_data: UserCreate, db: Session = Depends(get_db)):
    # A. Check if email already exists
    db_user = db.query(User).filter(User.email == user_data.email).first()
    if db_user:
        raise HTTPException(
            status_code=400, 
            detail="Email already registered. Please log in."
        )

    # B. Hash the password
    hashed_pw = get_password_hash(user_data.password)

    # C. Calculate Credits based on Plan (Optional)
    initial_credits = 20  # Default free credits
    if user_data.plan.lower() == "pro":
        initial_credits = 1000
    elif user_data.plan.lower() == "premium":
        initial_credits = 5000

    # D. Create the User in DB
    new_user = User(
        email=user_data.email,
        hashed_password=hashed_pw,
        full_name=user_data.full_name,
        provider="local",         # Mark as local user
        plan=user_data.plan,
        credits=initial_credits
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    # E. Auto-Login (Return Token)
    access_token = create_access_token(data={"sub": new_user.email})
    return {"access_token": access_token, "token_type": "bearer"}

def get_client_ip(request: Request):
    # Check if behind a proxy (like Nginx/Cloudflare)
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0]
    return request.client.host

# ==========================================
# 2. EMAIL/PASSWORD LOGIN
# ==========================================
@router.post("/login")
def login(
    request: Request,
    login_data: UserLogin, 
    db: Session = Depends(get_db)
):
    # A. Find User
    user = db.query(User).filter(User.email == login_data.email).first()
    
    # B. Verify Password
    if not user or not user.hashed_password or not verify_password(login_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # --- NEW CODE: Capture & Save IP ---
    try:
        user_ip = get_client_ip(request) # Get the IP
        user.last_login_ip = user_ip     # Update the user model
        db.commit()                      # Save to SQLite
        db.refresh(user)                 # Refresh instance (optional)
    except Exception as e:
        # Don't fail login just because IP tracking failed
        print(f"Failed to save IP: {e}") 
    # -----------------------------------

    # C. Generate Token
    access_token = create_access_token(data={"sub": user.email})
    
    # Optional: You can return the detected IP to debug on frontend
    return {
        "access_token": access_token, 
        "token_type": "bearer",
        "ip_detected": user.last_login_ip 
    }


# ==========================================
# 3. GOOGLE OAUTH
# ==========================================
@router.get("/google/login")
async def google_login():
    # Helper to prevent warnings
    async with google_sso:
        return await google_sso.get_login_redirect()

@router.get("/google/callback")
async def google_callback(request: Request, db: Session = Depends(get_db)):
    try:
        async with google_sso:
            google_user = await google_sso.verify_and_process(request)
        
        db_user = db.query(User).filter(User.email == google_user.email).first()
        
        if not db_user:
            # Create Google User (No Password)
            db_user = User(
                email=google_user.email,
                full_name=google_user.display_name,
                profile_pic=google_user.picture,
                provider="google",
                credits=20,
                plan="free"
            )
            db.add(db_user)
            db.commit()
            db.refresh(db_user)
            
        access_token = create_access_token(data={"sub": db_user.email})
        
        frontend_url = f"http://localhost:3000/auth-success?token={access_token}"
        return RedirectResponse(url=frontend_url)
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# ==========================================
# 4. UTILITIES
# ==========================================
@router.get("/logout")
async def logout():
    return {"message": "Logged out successfully"}

@router.get("/me")
def read_users_me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "email": current_user.email,
        "full_name": current_user.full_name,
        "credits": current_user.credits,
        "profile_pic": current_user.profile_pic,
        "plan": current_user.plan
    }