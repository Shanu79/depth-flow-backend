import os
import random
import requests
import random
from datetime import datetime, timedelta, timezone
from typing import Optional # Added
from pathlib import Path
from dotenv import load_dotenv
import shutil

from fastapi import APIRouter, Depends, HTTPException, Request, status, File, UploadFile, Form
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from fastapi_sso.sso.google import GoogleSSO
from pathlib import Path
from dotenv import load_dotenv
import secrets

from database import get_db
from models import User
from schemas import UserCreate, UserLogin, OTPVerify
from auth import (
    create_access_token, 
    get_current_user, 
    get_password_hash,
    verify_password 
)

from pydantic import BaseModel, EmailStr
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

router = APIRouter(prefix="/auth", tags=["Authentication"])

# Force Load Env
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

# Config
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
CALLBACK_URL = os.getenv("GOOGLE_REDIRECT_URI")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

# ZeptoMail Config
ZEPTOMAIL_SEND_TOKEN = os.getenv("ZEPTOMAIL_SEND_TOKEN")
ZEPTOMAIL_FROM_ADDRESS = os.getenv("ZEPTOMAIL_FROM_ADDRESS","noreply@depthflow.io")
ZEPTOMAIL_FROM_NAME = os.getenv("ZEPTOMAIL_FROM_NAME", "Depthflow AI")
# NEW: Load the API key from .env securely
ZEPTOMAIL_API_KEY = os.getenv("ZEPTOMAIL_API_KEY") 

google_sso = GoogleSSO(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, CALLBACK_URL)

# NEW: Missing Schema definition
class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

# ==========================================
# HELPER: SEND OTP EMAIL VIA ZEPTOMAIL API
# ==========================================
def send_otp_email(receiver_email: str, otp: str):
    url = "https://api.zeptomail.in/v1.1/email"

    headers = {
        'accept': "application/json",
        'content-type': "application/json",
        'authorization': "Zoho-enczapikey PHtE6r1eE7rui24npEcJsfK+HpKiPI4m9e42KFNPtN5GXv4FGk1U/9l9lDLlrhsqAKURFPLOzIlr5L6c4e+CITrvZzxIWWqyqK3sx/VYSPOZsbq6x00Vt1oedUTVU4PsddFq1SDfu97eNA==",
    }
    
    # Modern, Dark-Mode HTML Email Template
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="margin: 0; padding: 0; background-color: #050511; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
        <table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="background-color: #050511; padding: 40px 20px;">
            <tr>
                <td align="center">
                    <table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="max-width: 600px; background-color: #111218; border: 1px solid #1f2937; border-radius: 16px; overflow: hidden; box-shadow: 0 4px 25px rgba(0, 0, 0, 0.5);">
                        
                        <tr>
                            <td align="center" style="padding: 40px 40px 20px 40px;">
                                <h1 style="color: #ffffff; font-size: 28px; font-weight: 800; margin: 0; letter-spacing: -0.5px;">
                                    DepthFlow <span style="color: #a855f7;">AI</span>
                                </h1>
                            </td>
                        </tr>

                        <tr>
                            <td style="padding: 0 40px 30px 40px;">
                                <p style="color: #e2e8f0; font-size: 16px; line-height: 1.6; margin: 0 0 20px 0; text-align: center;">
                                    You are one step away from accessing your account. Please use the verification code below to complete your sign-in process.
                                </p>
                            </td>
                        </tr>

                        <tr>
                            <td align="center" style="padding: 0 40px 40px 40px;">
                                <table width="100%" cellpadding="0" cellspacing="0" role="presentation">
                                    <tr>
                                        <td align="center" style="background-color: #1e1b4b; border: 1px solid #4c1d95; border-radius: 12px; padding: 24px;">
                                            <div style="font-size: 36px; font-weight: 700; color: #ffffff; letter-spacing: 8px; margin: 0;">
                                                {otp}
                                            </div>
                                        </td>
                                    </tr>
                                </table>
                            </td>
                        </tr>

                        <tr>
                            <td align="center" style="padding: 0 40px 40px 40px;">
                                <p style="color: #64748b; font-size: 14px; line-height: 1.5; margin: 0;">
                                    This code is valid for <strong>10 minutes</strong>.<br>
                                    If you did not request this code, please ignore this email or contact support.
                                </p>
                            </td>
                        </tr>
                        
                        <tr>
                            <td style="height: 4px; background: linear-gradient(to right, #3b82f6, #8b5cf6); background-color: #8b5cf6;"></td>
                        </tr>
                    </table>

                    <table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="max-width: 600px;">
                        <tr>
                            <td align="center" style="padding: 20px 0;">
                                <p style="color: #475569; font-size: 12px; margin: 0;">
                                    &copy; {datetime.now(timezone.utc).year} Depthflow AI. All rights reserved.
                                </p>
                            </td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """

    payload = {
        "from": {
            "address": ZEPTOMAIL_FROM_ADDRESS,
            "name": ZEPTOMAIL_FROM_NAME
        },
        "to": [
            {
                "email_address": {
                    "address": receiver_email
                }
            }
        ],
        "subject": "Your Depthflow AI Verification Code",
        "htmlbody": html_content,
        "textbody": f"Welcome to Depthflow! Your verification code is: {otp}\n\nThis code will expire in 10 minutes."
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status() 
    except requests.exceptions.RequestException as e:
        print(f"Failed to send ZeptoMail: {e}")


# ==========================================
# 1. EMAIL/PASSWORD REGISTRATION (Sends OTP)
# ==========================================
@router.post("/register")
def register(user_data: UserCreate, db: Session = Depends(get_db)):
    db_user = db.query(User).filter(User.email == user_data.email).first()
    
    # Generate 6-digit OTP and set expiry to 10 minutes from now
    generated_otp = str(random.randint(100000, 999999))
    expiry_time = datetime.now(timezone.utc) + timedelta(minutes=10)

    if db_user:
        if db_user.is_verified:
            raise HTTPException(status_code=400, detail="Email already registered. Please log in.")
        else:
            # If user exists but isn't verified, resend a new OTP
            db_user.otp = generated_otp
            db_user.otp_expiry = expiry_time
            db.commit()
            send_otp_email(db_user.email, generated_otp)
            return {"message": "Unverified account found. A new OTP has been sent to your email."}

    # Hash the password
    hashed_pw = get_password_hash(user_data.password)

    # Create User (is_verified defaults to False)
    new_user = User(
        email=user_data.email,
        hashed_password=hashed_pw,
        full_name=user_data.full_name,
        provider="local",
        plan="Free",           
        billing_cycle="monthly",  
        subscription_status="active",
        is_verified=False,        
        otp=generated_otp,        
        otp_expiry=expiry_time    
    )
    
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    # Send the email via ZeptoMail
    send_otp_email(new_user.email, generated_otp)

    return {"message": "Registration successful. Please check your email for the OTP."}


# ==========================================
# 1b. VERIFY OTP (Returns Token)
# ==========================================
@router.post("/verify-otp")
def verify_otp(otp_data: OTPVerify, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == otp_data.email).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    if user.is_verified:
        raise HTTPException(status_code=400, detail="User is already verified.")
    if user.otp != otp_data.otp:
        raise HTTPException(status_code=400, detail="Invalid OTP.")
    
    now = datetime.now(timezone.utc)
    otp_expiry = user.otp_expiry.replace(tzinfo=timezone.utc) if user.otp_expiry.tzinfo is None else user.otp_expiry
    
    if otp_expiry < now:
        raise HTTPException(status_code=400, detail="OTP has expired. Please request a new one.")

    # Success! Mark as verified and clear OTP
    user.is_verified = True
    user.otp = None
    user.otp_expiry = None
    db.commit()

    # Generate Token and auto-login
    access_token = create_access_token(data={"sub": user.email})
    return {
        "message": "Email verified successfully.",
        "access_token": access_token, 
        "token_type": "bearer"
    }


def get_client_ip(request: Request):
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
    user = db.query(User).filter(User.email == login_data.email).first()
    
    if not user or not user.hashed_password or not verify_password(login_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # --- BLOCK UNVERIFIED USERS ---
    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email not verified. Please check your inbox or register again to receive a new OTP."
        )

    try:
        user_ip = get_client_ip(request) 
        user.last_login_ip = user_ip     
        db.commit()                      
        db.refresh(user)                 
    except Exception as e:
        print(f"Failed to save IP: {e}") 

    access_token = create_access_token(data={"sub": user.email})
    
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
            db_user = User(
                email=google_user.email,
                full_name=google_user.display_name,
                profile_pic=google_user.picture,
                provider="google",
                plan="Free",
                subscription_status="active",
                billing_cycle="monthly",
                is_verified=True # Google users are inherently verified
            )
            db.add(db_user)
            db.commit()
            db.refresh(db_user)
            
        access_token = create_access_token(data={"sub": db_user.email})
        
        frontend_url = f"{FRONTEND_URL}/auth-success?token={access_token}"
        return RedirectResponse(url=frontend_url)
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    
# Schema for incoming Android payload
class GoogleTokenReq(BaseModel):
    id_token: str

# ==========================================
# 3b. GOOGLE ID TOKEN VERIFICATION (For Mobile / API)
# ==========================================
@router.post("/google/verify")
def verify_google_token(token_data: GoogleTokenReq, db: Session = Depends(get_db)):
    """
    Accepts a Google ID Token from an Android/iOS/Web client, verifies it, 
    and returns a standard JWT access token. Seamlessly merges accounts.
    """
    try:
        # 1. Verify the token with Google's servers
        idinfo = id_token.verify_oauth2_token(
            token_data.id_token, 
            google_requests.Request(), 
            GOOGLE_CLIENT_ID
        )

        # 2. Extract verified user info
        email = idinfo['email']
        full_name = idinfo.get('name', 'User')
        picture = idinfo.get('picture', None)

        # 3. Look up user in our database
        db_user = db.query(User).filter(User.email == email).first()

        if not db_user:
            # BRAND NEW USER: Create them
            db_user = User(
                email=email,
                full_name=full_name,
                profile_pic=picture,
                provider="google",
                plan="Free",
                subscription_status="active",
                billing_cycle="monthly",
                is_verified=True # Google guarantees the email is real
            )
            db.add(db_user)
            db.commit()
            db.refresh(db_user)
        else:
            # EXISTING USER: Syncing flow
            # If they registered via Email but are now using Google, we automatically 
            # mark them as verified since Google vouches for their email.
            dirty = False
            if not db_user.is_verified:
                db_user.is_verified = True
                dirty = True
            
            # Optionally sync their profile picture if they didn't have one
            if not db_user.profile_pic and picture:
                db_user.profile_pic = picture
                dirty = True
                
            if dirty:
                db.commit()

        # 4. Generate our standard app JWT token
        access_token = create_access_token(data={"sub": db_user.email})
        
        return {
            "access_token": access_token, 
            "token_type": "bearer",
            "message": "Google Sign-In Successful"
        }

    except ValueError:
        # Invalid token
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="Invalid or expired Google ID token."
        )

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
        "subscription_status": current_user.subscription_status,
        "subscription_id": current_user.subscription_id,
        "billing_cycle": current_user.billing_cycle,
        "api_key": current_user.api_key,
        "is_verified": current_user.is_verified
    }
    
@router.post("/regenerate-api-key")
def regenerate_api_key(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    new_key = f"df_{secrets.token_urlsafe(32)}"
    current_user.api_key = new_key
    db.commit()
    
    return {"status": "success", "api_key": new_key, "message": "API key regenerated successfully"}

# ==========================================
# 5. UPDATE PROFILE (Multipart/Form-Data)
# ==========================================
# Allowed MIME types
ALLOWED_IMAGE_TYPES = ["image/jpeg", "image/png", "image/webp"]

@router.post("/update-profile")
async def update_profile(
    full_name: Optional[str] = Form(None),
    email: Optional[EmailStr] = Form(None),
    profile_pic: Optional[UploadFile] = File(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Updates user's name, email, and/or profile picture.
    Handles multipart form data to allow file uploads.
    """
    try:
        dirty = False
        
        # 1. Update Full Name
        if full_name is not None:
            current_user.full_name = full_name
            dirty = True
            
        # 2. Update Email (Check for conflicts)
        if email is not None and email != current_user.email:
            existing_user = db.query(User).filter(User.email == email).first()
            if existing_user:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, 
                    detail="This email address is already in use."
                )
            current_user.email = email
            dirty = True
            
        # 3. Process Profile Picture Upload
        if profile_pic:
            # Create a unique filename using timestamp
            ext = os.path.splitext(profile_pic.filename)[1]
            filename = f"profile_{current_user.id}_{int(datetime.now().timestamp())}{ext}"
            
            # Directory where images are stored (Make sure your app serves this folder as static)
            upload_dir = "static/uploads/profile_pics"
            os.makedirs(upload_dir, exist_ok=True)
            
            file_path = os.path.join(upload_dir, filename)
            
            # Save the file locally
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(profile_pic.file, buffer)
            
            # Update the URL in the database
            # Update this to match your actual domain/serving logic
            current_user.profile_pic = f"https://api.depthflow.io/{file_path}"
            dirty = True

        if dirty:
            db.commit()
            db.refresh(current_user)

        # Return updated user info (matches UserProfileResponse on Android)
        return {
            "id": current_user.id,
            "email": current_user.email,
            "full_name": current_user.full_name,
            "credits": current_user.credits,
            "profile_pic": current_user.profile_pic,
            "plan": current_user.plan,
            "subscription_status": current_user.subscription_status,
            "billing_cycle": current_user.billing_cycle
        }

    except HTTPException:
        raise
    except Exception as e:
        # Use your actual logger here
        print(f"Profile Update Error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail="Failed to update profile info."
        )


# ==========================================
# 6. CHANGE PASSWORD
# ==========================================
@router.post("/change-password")
def change_password(
    request: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Securely changes the user's password after verifying the old one.
    """
    # 1. Verify the old password matches
    if not verify_password(request.old_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="The current password you entered is incorrect."
        )
    
    # 2. Hash and update the new password
    current_user.hashed_password = get_password_hash(request.new_password)
    db.commit()
    
    return {"status": "success", "message": "Password updated successfully."}