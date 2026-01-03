import os
import uuid
import time
import requests
import subprocess
import imageio_ffmpeg
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends, BackgroundTasks
from sqlalchemy.orm import Session
from database import get_db
from models import User
from auth import get_current_user

router = APIRouter(prefix="/ai", tags=["AI Generation"])

# CONFIGURATION
MEDIA_CLOUD_REST_API_BASE_URL = 'https://api.immersity.ai'
IMMERSITY_CLIENT_ID = os.getenv("IMMERSITY_CLIENT_ID")
IMMERSITY_CLIENT_SECRET = os.getenv("IMMERSITY_CLIENT_SECRET")
COST_PER_GENERATION = 20

# --- HELPER FUNCTIONS ---

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

def apply_watermark(input_url, output_path, watermark_path="assets/watermark.png"):
    """
    Downloads video first, then overlays watermark using python-embedded FFmpeg.
    """
    temp_input = f"temp_{uuid.uuid4()}.mp4"

    try:
        # 1. DOWNLOAD VIDEO FIRST (Fixes FFmpeg network errors)
        r = requests.get(input_url)
        with open(temp_input, 'wb') as f:
            f.write(r.content)

        # 2. GET FFMPEG PATH
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        
        # 3. CHECK WATERMARK
        if not os.path.exists(watermark_path):
            print(f"⚠️ Watermark missing. Saving original.")
            os.rename(temp_input, output_path)
            return

        # 4. RUN FFMPEG (Local file -> Local file)
        command = [
            ffmpeg_exe, '-y',
            '-i', temp_input,      # Input is now local file
            '-i', watermark_path,
            '-filter_complex', 'overlay=main_w-overlay_w-10:main_h-overlay_h-10',
            '-c:v', 'libx264', '-preset', 'ultrafast',
            '-c:a', 'copy',
            output_path
        ]
        
        subprocess.run(command, check=True, capture_output=True)
        
        # Clean up temp file
        if os.path.exists(temp_input):
            os.remove(temp_input)

    except Exception as e:
        # If it was a subprocess error, print stderr for debugging
        if isinstance(e, subprocess.CalledProcessError):
            print(f"FFmpeg Error Details: {e.stderr.decode()}")
        else:
            print(f"Processing Error: {str(e)}")
            
        # FALLBACK: Just save the downloaded file as output
        if os.path.exists(temp_input):
            if os.path.exists(output_path):
                os.remove(output_path)
            os.rename(temp_input, output_path)
        else:
            # If download failed, try streaming direct to output (last resort)
            r = requests.get(input_url)
            with open(output_path, 'wb') as f:
                f.write(r.content)

def cleanup_old_files(folder="static", age_limit=1800): 
    """Deletes files older than 30 mins."""
    now = time.time()
    if not os.path.exists(folder):
        return
    for filename in os.listdir(folder):
        file_path = os.path.join(folder, filename)
        if os.path.isfile(file_path):
            if os.stat(file_path).st_mtime < (now - age_limit):
                try: os.remove(file_path)
                except: pass

# --- ROUTES ---

@router.post("/generate-3d")
async def generate_3d(
    file: UploadFile = File(...),
    style: str = Form("Dolly"),
    depth: int = Form(5),
    speed: int = Form(5),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if current_user.credits < COST_PER_GENERATION:
        raise HTTPException(status_code=402, detail="❌ Not enough credits.")

    try:
        # 1. UPLOAD IMAGE
        token = get_immersity_token()
        upload_info = get_upload_url(token, file.filename, file.content_type)
        
        file.file.seek(0)
        file_content = await file.read()
        requests.put(upload_info['url'], data=file_content, headers={"Content-Type": file.content_type}).raise_for_status()
        
        input_image_url = upload_info['url']
        correlation_id = str(uuid.uuid4())

        # 2. GENERATE DISPARITY MAP
        disparity_payload = { "correlationId": correlation_id, "inputImageUrl": input_image_url }
        
        disp_response = requests.post(
            f'{MEDIA_CLOUD_REST_API_BASE_URL}/api/v1/disparity',
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=disparity_payload
        )
        disp_response.raise_for_status()
        input_disparity_url = disp_response.json().get('resultPresignedUrl')

        # 3. CONFIGURE PHYSICS
        intensity = float(depth)
        params = {"amplitudeX": 0, "amplitudeY": 0, "amplitudeZ": 0, "phaseX": 0, "phaseY": 0, "phaseZ": 0}

        if style == "Orbit":
            params["amplitudeX"] = intensity
            params["amplitudeY"] = intensity * 0.5
            params["phaseY"] = 0.25
        elif style == "Dolly":
            params["amplitudeZ"] = intensity
            params["amplitudeX"] = intensity * 0.1
        elif style == "Zoom":
            params["amplitudeZ"] = intensity * 1.2
        elif style == "Horizontal":
            params["amplitudeX"] = intensity
            
        animation_correlation_id = str(uuid.uuid4())

        # 4. GENERATE ANIMATION
        anim_payload = {
            "correlationId": animation_correlation_id,
            "inputImageUrl": input_image_url,
            "inputDisparityUrl": input_disparity_url,
            "animationLength": float(speed),
            **params
        }

        response = requests.post(
            f'{MEDIA_CLOUD_REST_API_BASE_URL}/api/v1/animation',
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=anim_payload,
            timeout=180
        )

        if response.status_code == 402: 
             raise HTTPException(status_code=503, detail="Service Maintenance (Upstream Quota).")
        
        if response.status_code != 201:
            raise HTTPException(status_code=response.status_code, detail=f"Immersity Error: {response.text}")

        immersity_video_url = response.json().get('resultPresignedUrl')

        # 5. WATERMARK & SAVE
        filename = f"{correlation_id}_branded.mp4"
        output_path = f"static/{filename}"
        
        apply_watermark(immersity_video_url, output_path)

        # 6. CLEANUP & RETURN
        background_tasks.add_task(cleanup_old_files)
        current_user.credits -= COST_PER_GENERATION
        db.commit()

        # USE THE ENV VAR FOR THE URL
        base_url = os.getenv("BASE_URL", "http://localhost:8000")
        final_url = f"{base_url}/static/{filename}"

        return {
            "status": "success", 
            "video_url": final_url,
            "remaining_credits": current_user.credits
        }

    except Exception as e:
        print(f"Server Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))