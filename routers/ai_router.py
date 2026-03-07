import os
import uuid
import time
import httpx
import subprocess
import imageio_ffmpeg
import shutil
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends, BackgroundTasks, Request
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
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change_this_to_a_r@ndom_string")
COST_PER_GENERATION = 20

# --- ASYNC HELPER FUNCTIONS ---
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

# --- SYNC HELPER FUNCTIONS (For Background Tasks) ---
def save_video_direct(input_url, output_path):
    """Downloads the video directly using httpx.stream for memory safety."""
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
        # 1. STREAM DOWNLOAD TO DISK
        with httpx.Client() as client:
            with client.stream("GET", input_url) as r:
                r.raise_for_status()
                with open(temp_input, 'wb') as f:
                    for chunk in r.iter_bytes(chunk_size=8192):
                        f.write(chunk)

        # 2. GET FFMPEG PATH
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

        # 3. RUN FFMPEG
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
                with httpx.Client() as client:
                    with client.stream("GET", input_url) as r:
                        with open(output_path, 'wb') as f:
                            for chunk in r.iter_bytes(chunk_size=8192):
                                f.write(chunk)
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

# --- BACKGROUND WEBHOOK PROCESSOR ---
def process_finished_video_no_db_state(user_id: int, should_watermark: bool, immersity_video_url: str):
    """Processes the video in the background and saves it to the DB upon completion."""
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

        # Create DB record ONLY when finished
        new_history = GenerationHistory(
            user_id=user_id,
            video_url=final_url,
            created_at=datetime.utcnow()
        )
        db.add(new_history)
        db.commit()

    except Exception as e:
        print(f"❌ Webhook Processing Failed: {e}")
        # Refund credits if processing fails
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.credits += COST_PER_GENERATION
            db.commit()
    finally:
        db.close()


# --- 1. INITIATOR ENDPOINT ---
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

    # Deduct credits upfront
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
            correlation_id = str(uuid.uuid4())

            disparity_payload = { "correlationId": correlation_id, "inputImageUrl": input_image_url }
            disp_response = await client.post(
                f'{MEDIA_CLOUD_REST_API_BASE_URL}/api/v1/disparity',
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=disparity_payload,
                timeout=60.0
            )
            if disp_response.status_code >= 400:
                raise Exception(f"Immersity Disparity Error: {disp_response.text}")
                
            input_disparity_url = disp_response.json().get('resultPresignedUrl')

            # Configure Physics
            intensity = max(min((float(depth) * (float(speed) / 5.0) * (float(duration) / 5.0)), 10.0), 0.5)
            params = {"amplitudeX": 0, "amplitudeY": 0, "amplitudeZ": 0, "phaseX": 0, "phaseY": 0, "phaseZ": 0}

            if style == "Orbit":
                params.update({"amplitudeX": intensity * 1.0, "amplitudeY": intensity * 0.5, "phaseY": 0.25})
            elif style == "Dolly":
                params.update({"amplitudeX": intensity * 0.8, "amplitudeZ": intensity * 0.3, "phaseX": 0.0})
            elif style == "Zoom":
                params.update({"amplitudeZ": intensity * 1.0, "amplitudeX": 0.0})

            # Setup Webhook Parameters
            is_free_plan = current_user.plan.lower() == "free"
            is_ex_subscriber = current_user.subscription_status == "canceled"
            should_watermark = is_free_plan and not is_ex_subscriber
            
            base_url = os.getenv("BASE_URL", "https://your-domain.com") 
            webhook_url = f"{base_url}/ai/webhook/immersity?user_id={current_user.id}&watermark={'true' if should_watermark else 'false'}&token={WEBHOOK_SECRET}"

            anim_payload = {
                "correlationId": str(uuid.uuid4()),
                "inputImageUrl": input_image_url,
                "inputDisparityUrl": input_disparity_url,
                "animationLength": float(duration), 
                "webhookUrl": webhook_url, 
                **params
            }

            anim_response = await client.post(
                f'{MEDIA_CLOUD_REST_API_BASE_URL}/api/v1/animation',
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=anim_payload,
                timeout=30.0
            )
            
            if anim_response.status_code >= 400:
                raise Exception(f"Immersity Animation Error: {anim_response.text}")

        # Queue file cleanup
        background_tasks.add_task(cleanup_old_files)

        return {
            "status": "processing", 
            "message": "Your 3D video is generating. It will appear in your history shortly.",
            "remaining_credits": current_user.credits
        }

    except Exception as e:
        print(f"Server/Upstream Error: {e}")
        # Refund credits if Immersity failed upfront
        current_user.credits += COST_PER_GENERATION
        db.commit()
        raise HTTPException(status_code=502, detail="Upstream AI provider failed. Your credits have been refunded.")


# --- 2. WEBHOOK LISTENER ---
@router.post("/webhook/immersity")
async def immersity_webhook(
    request: Request,
    user_id: int, 
    watermark: str, 
    token: str,
    background_tasks: BackgroundTasks
):
    """Listens for completed videos from Immersity."""
    if token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook token")

    payload = await request.json()
    final_video_url = payload.get('resultPresignedUrl') 

    if final_video_url:
        should_watermark = watermark.lower() == 'true'
        background_tasks.add_task(process_finished_video_no_db_state, user_id, should_watermark, final_video_url)
    
    return {"status": "received"}
    

# --- 3. GET HISTORY ---
@router.get("/history")
async def get_history(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    history = db.query(GenerationHistory)\
        .filter(GenerationHistory.user_id == current_user.id)\
        .order_by(GenerationHistory.created_at.desc())\
        .limit(20)\
        .all()
    
    results = []
    for item in history:
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