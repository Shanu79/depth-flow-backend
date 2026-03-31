import os
import uuid
import time
from datetime import datetime
import json
import httpx
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends, BackgroundTasks
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from database import get_db
from models import User, GenerationHistory
from auth import get_current_user

router = APIRouter(prefix="/ai/depthflow", tags=["DepthFlow Generation"])

# --- CONFIGURATION ---
DEPTHFLOW_ENGINE_URL = os.getenv("DEPTHFLOW_ENGINE_URL")
DEPTHFLOW_SECRET_KEY = os.getenv("DEPTHFLOW_SECRET_KEY", "your-super-secret-internal-key")

# 1. ACTIVATED CREDIT COST
# We use an environment variable so you can adjust pricing without changing code, defaulting to 20 credits.
COST_PER_GENERATION = int(os.getenv("DEPTHFLOW_API_COST", 20)) 

# --- HELPER FUNCTION ---
def cleanup_old_files(folder="static", age_limit=1800): 
    now = time.time()
    if not os.path.exists(folder): return
    for filename in os.listdir(folder):
        file_path = os.path.join(folder, filename)
        if os.path.isfile(file_path) and os.stat(file_path).st_mtime < (now - age_limit):
            try: os.remove(file_path)
            except: pass

# --- THE ENDPOINT ---
@router.post("/generate-3d")
async def generate_depthflow(
    file: UploadFile = File(...),
    payload: str = Form(...), 
    request_source: str = Form("api"),  # <--- NEW: Defaults to "api" if not provided
    background_tasks: BackgroundTasks = BackgroundTasks(),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # 2. ENHANCED VALIDATION
    if current_user.credits < COST_PER_GENERATION:
        raise HTTPException(
            status_code=402, 
            detail=f"❌ Payment Required: This API call costs {COST_PER_GENERATION} credits. You currently have {current_user.credits} credits."
        )

    correlation_id = str(uuid.uuid4())
    temp_raw_video = f"temp_raw_{correlation_id}.mp4"
    filename = f"depthflow_{correlation_id}_clean.mp4"
    output_path = f"static/{filename}"

    try:
        engine_payload = json.loads(payload)

        # Reset file pointer just in case it was read elsewhere
        file.file.seek(0)
        file_content = await file.read()
        
        async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
            response = await client.post(
                DEPTHFLOW_ENGINE_URL,
                headers={"x-api-key": DEPTHFLOW_SECRET_KEY},
                files={"image": (file.filename, file_content, file.content_type)},
                data={"payload": json.dumps(engine_payload)}
            )
            response.raise_for_status()

            with open(temp_raw_video, 'wb') as f:
                f.write(response.content)

        os.rename(temp_raw_video, output_path)

        base_url = os.getenv("BASE_URL", "http://localhost:8000")
        final_url = f"{base_url}/static/{filename}"

        # --- DYNAMIC SOURCE TAGGING ---
        new_history = GenerationHistory(
            user_id=current_user.id, 
            video_url=final_url, 
            created_at=datetime.utcnow(),
            source=request_source  # <--- Saves "api" or "workspace" dynamically
        )
        db.add(new_history)
        
        # 3. DEDUCT CREDITS 
        current_user.credits -= COST_PER_GENERATION
        
        background_tasks.add_task(cleanup_old_files)
        db.commit() # Saves the history AND the new credit balance

        # 4. RETURN USAGE HEADERS
        return FileResponse(
            path=output_path, 
            media_type="video/mp4", 
            filename=filename,
            headers={
                "X-Status": "success", 
                "X-Workspace": "depthflow", 
                "X-Plan": current_user.plan,
                "X-Cost": str(COST_PER_GENERATION), 
                "X-Remaining-Credits": str(current_user.credits),
                "X-History-ID": str(new_history.id),
                "X-Video-URL": final_url
            }
        )

    except httpx.HTTPStatusError as he:
        print(f"Engine Error: {he.response.text}")
        if os.path.exists(temp_raw_video): os.remove(temp_raw_video)
        raise HTTPException(status_code=500, detail="The custom rendering engine failed.")
    except Exception as e:
        print(f"Server Error (DepthFlow): {e}")
        if os.path.exists(temp_raw_video): os.remove(temp_raw_video)
        raise HTTPException(status_code=500, detail=str(e))

# --- NEW ENDPOINT: GET HISTORY ---
@router.get("/logs")
async def get_api_logs(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # Fetch user's generation history, filter by API source, ordered by newest first
    history = db.query(GenerationHistory)\
        .filter(GenerationHistory.user_id == current_user.id)\
        .filter(GenerationHistory.source == "api")\
        .order_by(GenerationHistory.created_at.desc())\
        .all()
    
    logs = []
    for item in history:
        # Format the data to match the frontend expectations
        logs.append({
            "id": f"df_req_{item.id}{str(uuid.uuid4())[:4]}", 
            "status": "Success", 
            "credits": COST_PER_GENERATION,
            "duration": "~1200ms", 
            "time": item.created_at.strftime("%b %d, %I:%M %p")
        })
        
    return logs

@router.get("/billing")
async def get_billing_info(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Returns the current user's Workspace and API subscription details.
    """
    # Format the dates safely if they exist
    platform_end_date = current_user.subscription_end_date.strftime("%b %d, %Y") if current_user.subscription_end_date else "N/A"
    api_end_date = current_user.api_subscription_end_date.strftime("%b %d, %Y") if current_user.api_subscription_end_date else "N/A"

    return {
        # Shared Ledger
        "credits": current_user.credits,
        
        # Workspace/Platform Subscription Details
        "platform": {
            "plan": current_user.plan.capitalize() if current_user.plan else "Free",
            "billing_cycle": current_user.billing_cycle or "None",
            "status": current_user.subscription_status.capitalize() if current_user.subscription_status else "Inactive",
            "next_billing_date": platform_end_date,
            "is_active": current_user.subscription_status == "active" 
        },
        
        # API Subscription Details
        "api": {
            "plan": current_user.api_plan.capitalize() if current_user.api_plan else "Free",
            "billing_cycle": current_user.api_billing_cycle or "None",
            "status": current_user.api_subscription_status.capitalize() if current_user.api_subscription_status else "Inactive",
            "next_billing_date": api_end_date,
            "is_active": current_user.api_subscription_status == "active" 
        }
    }