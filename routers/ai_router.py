import os
import uuid
import time
import httpx
import subprocess
import imageio_ffmpeg
import asyncio
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends, BackgroundTasks
from sqlalchemy.orm import Session
from database import get_db, SessionLocal
from models import User, GenerationHistory
from datetime import datetime, timedelta
from auth import get_current_user

router = APIRouter(prefix="/ai", tags=["AI Generation"])

# --- CONFIGURATION ---
MEDIA_CLOUD_REST_API_BASE_URL = 'https://api.immersity.ai'
IMMERSITY_CLIENT_ID = os.getenv("IMMERSITY_CLIENT_ID")
IMMERSITY_CLIENT_SECRET = os.getenv("IMMERSITY_CLIENT_SECRET")
COST_PER_GENERATION = 20

# --- 1. ASYNC HELPER FUNCTIONS ---
async def get_immersity_token(client: httpx.AsyncClient):
    url = "https://auth.immersity.ai/auth/realms/immersity/protocol/openid-connect/token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": IMMERSITY_CLIENT_ID,
        "client_secret": IMMERSITY_CLIENT_SECRET,
    }
    response = await client.post(url, data=payload)
    response.raise_for_status()
    return response.json()["access_token"]

async def get_upload_url(client: httpx.AsyncClient, token: str, filename: str, content_type: str):
    url = f"{MEDIA_CLOUD_REST_API_BASE_URL}/api/v1/get-upload-url"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"fileName": filename, "mediaType": content_type}
    response = await client.get(url, headers=headers, params=params)
    response.raise_for_status()
    return response.json()

async def poll_presigned_url(client: httpx.AsyncClient, url: str, max_retries=60, sleep_time=2.0):
    for _ in range(max_retries):
        try:
            async with client.stream("GET", url) as response:
                if response.status_code == 200:
                    content_length = int(response.headers.get("content-length", 0))
                    if content_length > 1024: 
                        return True
        except Exception:
            pass
        await asyncio.sleep(sleep_time)
    return False

# --- NEW: RETRY LOGIC HELPER ---
async def call_immersity_with_retry(client: httpx.AsyncClient, url: str, headers: dict, json_payload: dict, max_retries: int = 3):
    """Wraps Immersity API calls in an exponential backoff retry loop for resilience against 500 errors."""
    for attempt in range(max_retries):
        response = await client.post(url, headers=headers, json=json_payload, timeout=30.0)
        
        # If successful or a client error (4xx like bad input), return immediately
        if response.status_code < 500:
            response.raise_for_status()
            return response
            
        # If Immersity throws a 500, log it and retry
        print(f"⚠️ Immersity API Error {response.status_code} on {url} (Attempt {attempt + 1}/{max_retries})")
        if attempt == max_retries - 1:
            response.raise_for_status() # Raise on the final failed attempt
            
        # Exponential backoff: sleep 1s, then 2s, etc.
        await asyncio.sleep(2 ** attempt)

# --- 2. SYNC VIDEO PROCESSING FUNCTIONS ---
# ... (Keep save_video_direct, apply_watermark, process_and_save_video, and cleanup_old_files exactly the same as your previous code) ...

def save_video_direct(input_url, output_path):
    try:
        with httpx.Client() as client:
            with client.stream("GET", input_url) as r:
                r.raise_for_status()
                with open(output_path, 'wb') as f:
                    for chunk in r.iter_bytes(chunk_size=8192):
                        f.write(chunk)
    except Exception as e:
        print(f"❌ Failed to download video: {e}")

def apply_watermark(input_url, output_path, watermark_path="assets/watermark.png"):
    temp_input = f"temp_{uuid.uuid4()}.mp4"
    try:
        with httpx.Client() as client:
            with client.stream("GET", input_url) as r:
                r.raise_for_status()
                with open(temp_input, 'wb') as f:
                    for chunk in r.iter_bytes(chunk_size=8192):
                        f.write(chunk)

        try:
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            os.rename(temp_input, output_path)
            return

        if not os.path.exists(watermark_path):
            os.rename(temp_input, output_path)
            return

        command = [
            ffmpeg_exe, '-y', '-i', temp_input, '-i', watermark_path,
            '-filter_complex', '[1:v][0:v]scale2ref[wm][vid];[vid][wm]overlay=0:0',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '1',         
            '-c:a', 'copy', '-max_muxing_queue_size', '1024', output_path
        ]
        subprocess.run(command, check=True, capture_output=True)
        if os.path.exists(temp_input):
            os.remove(temp_input)

    except Exception as e:
        print(f"⚠️ Video Processing Failed: {str(e)}")
        if os.path.exists(temp_input):
            os.rename(temp_input, output_path)

def process_and_save_video(user_id: int, immersity_video_url: str, should_watermark: bool):
    db = SessionLocal() 
    try:
        correlation_id = str(uuid.uuid4())
        filename = f"{correlation_id}_{'branded' if should_watermark else 'clean'}.mp4"
        output_path = f"static/{filename}"

        if should_watermark:
             apply_watermark(immersity_video_url, output_path)
        else:
             save_video_direct(immersity_video_url, output_path)

        base_url = os.getenv("BASE_URL", "http://localhost:8000")
        final_url = f"{base_url}/static/{filename}"

        new_history = GenerationHistory(user_id=user_id, video_url=final_url, created_at=datetime.utcnow())
        db.add(new_history)
        db.commit()

    except Exception as e:
        print(f"❌ Processing Thread Failed: {e}")
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.credits += COST_PER_GENERATION
            # Pass the error to the DB Workaround
            error_msg = f"FAILED: Internal processing error. Your {COST_PER_GENERATION} credits have been refunded."
            db.add(GenerationHistory(user_id=user_id, video_url=error_msg, created_at=datetime.utcnow()))
            db.commit()
    finally:
        db.close()

def cleanup_old_files(folder="static", age_limit=1800): 
    now = time.time()
    if not os.path.exists(folder): return
    for filename in os.listdir(folder):
        file_path = os.path.join(folder, filename)
        if os.path.isfile(file_path) and os.stat(file_path).st_mtime < (now - age_limit):
            try: os.remove(file_path)
            except: pass


# --- 3. THE CORE BACKGROUND ORCHESTRATOR ---
async def background_generation_task(
    user_id: int, token: str, input_image_url: str, 
    depth: int, speed: int, duration: int, style: str, should_watermark: bool
):
    try:
        async with httpx.AsyncClient() as client:
            # 1. Start Disparity (WITH RETRIES)
            disp_url = f'{MEDIA_CLOUD_REST_API_BASE_URL}/api/v1/disparity'
            disp_payload = {"correlationId": str(uuid.uuid4()), "inputImageUrl": input_image_url}
            
            disp_response = await call_immersity_with_retry(
                client, disp_url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, json_payload=disp_payload
            )
            input_disparity_url = disp_response.json().get('resultPresignedUrl')

            # 2. POLL Disparity
            is_disparity_ready = await poll_presigned_url(client, input_disparity_url)
            if not is_disparity_ready:
                raise Exception("Disparity generation timed out.")

            # 3. Configure Physics
            intensity = max(min((float(depth) * (float(speed) / 5.0) * (float(duration) / 5.0)), 10.0), 0.5)
            params = {
                "amplitudeX": 0.0, "amplitudeY": 0.0, "amplitudeZ": 0.0, 
                "phaseX": 0.0, "phaseY": 0.0, "phaseZ": 0.0
            }

            if style == "Orbit": params.update({"amplitudeX": intensity * 1.0, "amplitudeY": intensity * 0.5, "phaseY": 0.25})
            elif style == "Dolly": params.update({"amplitudeX": intensity * 0.8, "amplitudeZ": intensity * 0.3, "phaseX": 0.0})
            elif style == "Zoom": params.update({"amplitudeZ": intensity * 1.0, "amplitudeX": 0.0})

            # 4. Start Animation (WITH RETRIES)
            anim_url = f'{MEDIA_CLOUD_REST_API_BASE_URL}/api/v1/animation'
            anim_payload = {
                "correlationId": str(uuid.uuid4()), "inputImageUrl": input_image_url,
                "inputDisparityUrl": input_disparity_url, "animationLength": float(duration), **params
            }

            anim_response = await call_immersity_with_retry(
                client, anim_url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, json_payload=anim_payload
            )
            immersity_video_url = anim_response.json().get('resultPresignedUrl')

            # 5. POLL Animation
            is_anim_ready = await poll_presigned_url(client, immersity_video_url, max_retries=90)
            if not is_anim_ready:
                raise Exception("Animation video generation timed out.")

            # 6. Push heavy processing (FFMPEG) to an isolated thread
            await asyncio.to_thread(process_and_save_video, user_id, immersity_video_url, should_watermark)

    except Exception as e:
        print(f"❌ Background Orchestrator Failed: {e}")
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                # Refund the user
                user.credits += COST_PER_GENERATION
                
                # --- DB WORKAROUND FOR ERROR STATE ---
                # We save the error message as the "video_url". 
                # The frontend will read this and display an error card instead of a video player.
                error_msg = f"FAILED: Third-party AI service is currently unavailable. Your {COST_PER_GENERATION} credits have been refunded."
                new_history = GenerationHistory(
                    user_id=user_id,
                    video_url=error_msg,
                    created_at=datetime.utcnow()
                )
                db.add(new_history)
                db.commit()
        finally:
            db.close()


# --- 4. THE FAST API ENDPOINTS ---
@router.post("/generate-3d")
async def generate_3d(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    style: str = Form("Dolly"),
    depth: int = Form(5), 
    speed: int = Form(5), 
    duration: int = Form(5), 
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if current_user.credits < COST_PER_GENERATION:
        raise HTTPException(status_code=402, detail="❌ Not enough credits.")

    current_user.credits -= COST_PER_GENERATION
    db.commit()

    try:
        async with httpx.AsyncClient() as client:
            token = await get_immersity_token(client)
            upload_info = await get_upload_url(client, token, file.filename, file.content_type)
            
            file.file.seek(0)
            file_content = await file.read()
            await client.put(upload_info['url'], content=file_content, headers={"Content-Type": file.content_type})
            
            input_image_url = upload_info['url']

        is_free_plan = current_user.plan.lower() == "free"
        should_watermark = is_free_plan and current_user.subscription_status != "canceled"

        # Queue the heavy lifting!
        background_tasks.add_task(
            background_generation_task,
            user_id=current_user.id, token=token, input_image_url=input_image_url,
            depth=depth, speed=speed, duration=duration, style=style, should_watermark=should_watermark
        )
        background_tasks.add_task(cleanup_old_files)

        return {
            "status": "processing", 
            "message": "Your 3D video is generating. It will appear in your history shortly.",
            "remaining_credits": current_user.credits
        }

    except Exception as e:
        print(f"Server Error during Initialization: {e}")
        current_user.credits += COST_PER_GENERATION
        db.commit()
        raise HTTPException(
            status_code=502, 
            detail="The upstream AI provider is currently unreachable. Your credits have been refunded. Please try again later."
        )

@router.get("/history")
async def get_history(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    history = db.query(GenerationHistory).filter(GenerationHistory.user_id == current_user.id).order_by(GenerationHistory.created_at.desc()).limit(20).all()
    results = []
    
    for item in history:
        remaining = ((item.created_at + timedelta(minutes=30)) - datetime.utcnow()).total_seconds()
        
        # Parse our database workaround
        is_failed = item.video_url.startswith("FAILED:")
        
        results.append({
            "id": item.id,
            "video_url": None if is_failed else item.video_url, # Hide the URL if it's an error string
            "error_message": item.video_url.replace("FAILED: ", "") if is_failed else None,
            "is_failed": is_failed,
            "created_at": item.created_at.isoformat(),
            "expires_in_seconds": max(0, int(remaining)),
            "is_expired": remaining <= 0 and not is_failed # Failed records don't "expire"
        })
    return results