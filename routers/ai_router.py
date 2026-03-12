import os
import uuid
import time
import requests
import subprocess
import imageio_ffmpeg
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends, BackgroundTasks
from sqlalchemy.orm import Session
from database import get_db
from models import User, GenerationHistory
from datetime import datetime, timedelta
from auth import get_current_user
import shutil
import gc

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

def save_video_direct(input_url, output_path):
    """
    For PAID users: Downloads the video directly without watermarking.
    Uses memory-safe streaming to prevent crashes.
    """
    try:
        with requests.get(input_url, stream=True) as r:
            r.raise_for_status()
            with open(output_path, 'wb') as f:
                shutil.copyfileobj(r.raw, f)
    except Exception as e:
        print(f"❌ Failed to download video: {e}")
        raise HTTPException(status_code=500, detail="Failed to save video file.")

def apply_watermark(input_url, output_path, watermark_path="assets/watermark.png"):
    """
    Low-RAM Optimized Version: Applies watermark to the WHOLE video (Full Screen).
    """
    temp_input = f"temp_{uuid.uuid4()}.mp4"

    try:
        # 1. STREAM DOWNLOAD TO DISK (Keep RAM clean)
        with requests.get(input_url, stream=True) as r:
            r.raise_for_status()
            with open(temp_input, 'wb') as f:
                shutil.copyfileobj(r.raw, f)

        # 2. FORCE GARBAGE COLLECTION
        gc.collect()

        # 3. GET FFMPEG PATH
        try:
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        except:
            print("⚠️ FFmpeg binary not found. Saving original.")
            os.rename(temp_input, output_path)
            return

        if not os.path.exists(watermark_path):
            print(f"⚠️ Watermark missing at {watermark_path}. Saving original.")
            os.rename(temp_input, output_path)
            return

        # 4. RUN FFMPEG (LOW MEMORY MODE)
        # [1:v][0:v]scale2ref[wm][vid] -> Scales watermark (1) to match video (0) dimensions
        # [vid][wm]overlay=0:0 -> Applies scaled watermark over the video starting at top-left
        command = [
            ffmpeg_exe, '-y',
            '-i', temp_input, 
            '-i', watermark_path,
            '-filter_complex', '[1:v][0:v]scale2ref[wm][vid];[vid][wm]overlay=0:0',
            '-c:v', 'libx264', 
            '-preset', 'ultrafast',  
            '-threads', '1',         
            '-c:a', 'copy',
            '-max_muxing_queue_size', '1024', 
            output_path
        ]

        subprocess.run(command, check=True, capture_output=True)

        if os.path.exists(temp_input):
            os.remove(temp_input)

    except Exception as e:
        print(f"⚠️ Video Processing Failed: {str(e)}")
        # Fallback logic
        if os.path.exists(temp_input):
            if os.path.exists(output_path):
                os.remove(output_path)
            os.rename(temp_input, output_path)
        else:
            try:
                with requests.get(input_url, stream=True) as r:
                    with open(output_path, 'wb') as f:
                        shutil.copyfileobj(r.raw, f)
            except:
                pass

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

@router.post("/generate-3d")
async def generate_3d(
    file: UploadFile = File(...),
    style: str = Form("Dolly"),
    depth: int = Form(5),    # 1-10: How "deep" the 3D effect is (Base Amplitude)
    speed: int = Form(5),    # 1-10: How fast the camera moves (Velocity Multiplier)
    duration: int = Form(5), # 1-10: Length of the video in seconds
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

        # 3. CONFIGURE PHYSICS (The "Decoupling" Math)
        
        # Base Depth: The user's desired spatial scale (1-10)
        base_depth = float(depth)

        # Speed Factor: 
        # 5 is "1x" speed. 10 is "2x" speed.
        speed_multiplier = float(speed) / 5.0 

        # Duration Factor: 
        # A 10s video is naturally 2x slower than a 5s video.
        # We must double the amplitude to compensate and keep the speed constant.
        time_compensation = float(duration) / 5.0

        # Final Intensity Calculation
        # Amplitude = BaseDepth * DesiredVelocity * DurationCompensation
        raw_intensity = base_depth * speed_multiplier * time_compensation

        # API Safety Cap: Immersity usually limits amplitude to 10.0.
        # Note: If raw_intensity > 10, the video will physically hit the speed limit 
        # and might look slower than requested.
        intensity = min(raw_intensity, 10.0)
        intensity = max(intensity, 0.5) # Prevent 0 (no motion)

        # Initialize params
        params = {"amplitudeX": 0, "amplitudeY": 0, "amplitudeZ": 0, "phaseX": 0, "phaseY": 0, "phaseZ": 0}

        if style == "Orbit":
            # Circular motion
            params["amplitudeX"] = intensity * 1.0
            params["amplitudeY"] = intensity * 0.5
            params["phaseY"] = 0.25

        elif style == "Dolly":
            # Horizontal Truck/Slider (Distinct from Zoom)
            params["amplitudeX"] = intensity * 0.8
            params["amplitudeZ"] = intensity * 0.3
            params["phaseX"] = 0.0

        elif style == "Zoom":
            # Pure Z-axis depth
            params["amplitudeZ"] = intensity * 1.0 
            params["amplitudeX"] = 0.0
            
        animation_correlation_id = str(uuid.uuid4())

        # 4. GENERATE ANIMATION
        # We pass 'duration' to animationLength, but NOT 'speed'. 
        # Speed is baked into the 'params' (amplitudes) above.
        anim_payload = {
            "correlationId": animation_correlation_id,
            "inputImageUrl": input_image_url,
            "inputDisparityUrl": input_disparity_url,
            "animationLength": float(duration), 
            "convergence": 0,
            "gain": 0.1,
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

        # --- 5. WATERMARK LOGIC ---
        is_free_plan = current_user.plan.lower() == "free"
        is_ex_subscriber = current_user.subscription_status == "canceled"
        
        should_watermark = is_free_plan and not is_ex_subscriber

        if should_watermark:
            filename = f"{correlation_id}_branded.mp4"
            output_path = f"static/{filename}"
            apply_watermark(immersity_video_url, output_path)
        else:
            filename = f"{correlation_id}_clean.mp4"
            output_path = f"static/{filename}"
            save_video_direct(immersity_video_url, output_path)

        base_url = os.getenv("BASE_URL", "http://localhost:8000")
        final_url = f"{base_url}/static/{filename}"

        # --- DB INSERTION START ---
        new_history = GenerationHistory(
            user_id=current_user.id,
            video_url=final_url,
            created_at=datetime.utcnow()
        )
        db.add(new_history)
        # --- DB INSERTION END ---

        # 6. CLEANUP & RETURN
        background_tasks.add_task(cleanup_old_files)
        current_user.credits -= COST_PER_GENERATION
        db.commit() # This commits both the credit deduction AND the new history

        return {
            "status": "success", 
            "video_url": final_url,
            "plan": current_user.plan,
            "remaining_credits": current_user.credits,
            # Return the ID so frontend can update immediately
            "history_id": new_history.id 
        }

    except Exception as e:
        print(f"Server Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
# --- NEW ENDPOINT: GET HISTORY ---
@router.get("/history")
async def get_history(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # Fetch last 20 items, newest first
    history = db.query(GenerationHistory)\
        .filter(GenerationHistory.user_id == current_user.id)\
        .order_by(GenerationHistory.created_at.desc())\
        .limit(20)\
        .all()
    
    # Calculate expiry based on your 30 minute cleanup rule
    results = []
    for item in history:
        # Calculate remaining seconds
        expires_at = item.created_at + timedelta(minutes=30)
        remaining = (expires_at - datetime.utcnow()).total_seconds()
        
        is_expired = remaining <= 0
        
        results.append({
            "id": item.id,
            "video_url": item.video_url,
            "created_at": item.created_at.isoformat(),
            "expires_in_seconds": max(0, int(remaining)),
            "is_expired": is_expired
        })
        
    return results