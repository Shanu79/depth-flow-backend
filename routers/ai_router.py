import os
import uuid
import requests
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends
from sqlalchemy.orm import Session
from database import get_db
from models import User
from auth import get_current_user

router = APIRouter(prefix="/ai", tags=["AI Generation"])

MEDIA_CLOUD_REST_API_BASE_URL = 'https://api.immersity.ai'
IMMERSITY_CLIENT_ID = os.getenv("IMMERSITY_CLIENT_ID")
IMMERSITY_CLIENT_SECRET = os.getenv("IMMERSITY_CLIENT_SECRET")
COST_PER_GENERATION = 20

def get_immersity_token():
    url = "https://auth.immersity.ai/auth/realms/immersity/protocol/openid-connect/token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": IMMERSITY_CLIENT_ID,
        "client_secret": IMMERSITY_CLIENT_SECRET,
    }
    response = requests.post(url, data=payload)
    response.raise_for_status()
    return response.json()["access_token"]

def get_upload_url(token, filename, content_type):
    url = f"{MEDIA_CLOUD_REST_API_BASE_URL}/api/v1/get-upload-url"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"fileName": filename, "mediaType": content_type}
    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()
    return response.json()

@router.post("/generate-3d")
async def generate_3d(
    file: UploadFile = File(...),
    style: str = Form("Dolly"),
    depth: int = Form(5),
    speed: int = Form(5),
    current_user: User = Depends(get_current_user), # PROTECTED ROUTE
    db: Session = Depends(get_db)
):
    # 1. THE GATEKEEPER (Check Credits)
    if current_user.credits < COST_PER_GENERATION:
        raise HTTPException(status_code=402, detail="❌ Not enough credits. Please upgrade your plan.")

    try:
        # 2. CALL IMMERSITY
        token = get_immersity_token()
        upload_info = get_upload_url(token, file.filename, file.content_type)
        
        file.file.seek(0)
        file_content = await file.read()
        requests.put(upload_info['url'], data=file_content, headers={"Content-Type": file.content_type}).raise_for_status()

        correlation_id = str(uuid.uuid4())
        style_map = { "Dolly": "zoom-center", "Orbit": "circle", "Zoom": "dolly" }
        
        payload = {
            "correlationId": correlation_id,
            "inputImageUrl": upload_info['url'],
            "animationStyle": style_map.get(style, "circle"),
            "animationLength": float(speed),
            "amplitudeX": float(depth) / 10.0,
            "phaseX": 1.0
        }
        
        response = requests.post(
            f'{MEDIA_CLOUD_REST_API_BASE_URL}/api/v1/animation',
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=180
        )

        if response.status_code == 402: 
             raise HTTPException(status_code=503, detail="Service Maintenance (Upstream Quota).")
        
        if response.status_code != 201:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        # 3. THE TRANSACTION (Deduct Credits)
        current_user.credits -= COST_PER_GENERATION
        db.commit()

        return {
            "status": "success", 
            "video_url": response.json().get('resultPresignedUrl'),
            "remaining_credits": current_user.credits
        }

    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail=str(e))