import os
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from fastapi_sso.sso.google import GoogleSSO

from database import get_db
from models import User
from auth import create_access_token, get_current_user
from pathlib import Path
from dotenv import load_dotenv

router = APIRouter(prefix="/auth", tags=["Authentication"])

# Load environment variables from the .env file
load_dotenv()

# Google Config
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
CALLBACK_URL = os.getenv("GOOGLE_REDIRECT_URI")

google_sso = GoogleSSO(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, CALLBACK_URL)

@router.get("/google/login")
async def google_login():
    return await google_sso.get_login_redirect()

@router.get("/google/callback")
async def google_callback(request: Request, db: Session = Depends(get_db)):
    """
    1. Google returns User Info.
    2. We Check DB. If new, Create User (Give Credits).
    3. Generate JWT.
    4. Redirect to Frontend with Token.
    """
    try:
        async with google_sso:
            google_user = await google_sso.verify_and_process(request)
        
        db_user = db.query(User).filter(User.email == google_user.email).first()
        
        if not db_user:
            # CREATE NEW USER
            db_user = User(
                email=google_user.email,
                full_name=google_user.display_name,
                profile_pic=google_user.picture,
                provider="google",
                credits=50 # Free Plan
            )
            db.add(db_user)
            db.commit()
            db.refresh(db_user)
            
        # GENERATE JWT
        access_token = create_access_token(data={"sub": db_user.email})
        
        # REDIRECT TO FRONTEND (Append Token)
        # Frontend grabs this token from URL and saves to localStorage
        frontend_url = f"http://localhost:3000/auth-success?token={access_token}"
        return RedirectResponse(url=frontend_url)
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    
@router.get("/logout")
async def logout():
    # If you were using server-side cookies, you would clear them here.
    # Since we use JWTs, we just return a success signal.
    return {"message": "Logged out successfully"}

@router.get("/me")
def read_users_me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "email": current_user.email,
        "full_name": current_user.full_name,
        "credits": current_user.credits,
        "profile_pic": current_user.profile_pic
    }