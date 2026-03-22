import os
import uuid
import time
import httpx
import json
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends, BackgroundTasks
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from database import get_db
from models import User, GenerationHistory
from datetime import datetime
from auth import get_current_user

router = APIRouter(prefix="/ai/depthflow", tags=["DepthFlow Generation"])

# --- CONFIGURATION ---
DEPTHFLOW_ENGINE_URL = os.getenv("DEPTHFLOW_ENGINE_URL")
DEPTHFLOW_SECRET_KEY = os.getenv("DEPTHFLOW_SECRET_KEY", "your-super-secret-internal-key")
COST_PER_GENERATION = 0

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
    background_tasks: BackgroundTasks = BackgroundTasks(),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if current_user.credits < COST_PER_GENERATION:
        raise HTTPException(status_code=402, detail="❌ Not enough credits.")

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

        new_history = GenerationHistory(user_id=current_user.id, video_url=final_url, created_at=datetime.utcnow())
        db.add(new_history)
        
        background_tasks.add_task(cleanup_old_files)
        current_user.credits -= COST_PER_GENERATION
        db.commit()

        # Returns the actual file so the frontend/Swagger can download it easily
        return FileResponse(
            path=output_path, 
            media_type="video/mp4", 
            filename=filename,
            headers={
                "X-Status": "success", "X-Workspace": "depthflow", "X-Plan": current_user.plan,
                "X-Remaining-Credits": str(current_user.credits), "X-History-ID": str(new_history.id),
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