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

# frontend URL
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")


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

    # C. Create User (FORCE FREE PLAN)
    # We ignore 'user_data.plan' completely for security.
    new_user = User(
        email=user_data.email,
        hashed_password=hashed_pw,
        full_name=user_data.full_name,
        provider="local",
        
        # --- ENFORCE DEFAULTS ---
        plan="Free",              # Always start as Free
        credits=20,               # Give starter credits (e.g. 5)
        billing_cycle="monthly",  # Default to monthly
        subscription_status="active" # Free plan is always active
    )
    
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    # D. Auto-Login
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
            google_user = await google_sso.verify_and_process(
                request, 
                redirect_uri=CALLBACK_URL
            )
        
        db_user = db.query(User).filter(User.email == google_user.email).first()
        
        if not db_user:
            # --- CREATE NEW GOOGLE USER ---
            db_user = User(
                email=google_user.email,
                full_name=google_user.display_name,
                profile_pic=google_user.picture,
                provider="google",
                
                # --- ENFORCE DEFAULTS ---
                plan="Free",
                credits=20,
                subscription_status="active",
                billing_cycle="monthly"
            )
            db.add(db_user)
            db.commit()
            db.refresh(db_user)
            
        access_token = create_access_token(data={"sub": db_user.email})
        
        # Ensure you imported FRONTEND_URL from your .env config at the top
        frontend_url = f"{FRONTEND_URL}/auth-success?token={access_token}"
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
        "plan": current_user.plan,
        "is_admin": current_user.is_admin,
        "subscription_status": current_user.subscription_status
    }