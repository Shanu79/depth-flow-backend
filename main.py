import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
import uvicorn
from fastapi import FastAPI, Depends, Request  # Added Depends
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from database import engine, Base, get_db  # Added get_db

# --- IMPORT THE ROUTERS ---
from routers import auth_router, depthflow_ai_router, payments_router, admin_router, ai_router, contact_router, happy_hour_router, notifications_router
import notifications

# --- IMPORTS FOR THE ALIAS ROUTE ---
# Adjust these imports if your folder structure differs slightly
from routers.payments_router import verify_google_play_purchase, GooglePlayPurchaseReq
from models import User
from happy_hour import HappyHourError
from auth import get_current_user
from sqlalchemy.orm import Session

# 1. LOAD ENV VARS FIRST
load_dotenv()

# Create DB Tables
Base.metadata.create_all(bind=engine)

NOTIFY_SWEEP_SECONDS = int(os.getenv("NOTIFY_SWEEP_SECONDS", "60"))


# --- NOTIFICATION SWEEP (Happy Hour "ending soon" / "ended" / monthly-unlock notices) ---
# Runs inside the API process every 60 s. Idempotent (dedupe keys), so several workers or the cron script
# (cron_happy_hour_notify.py) can run it at the same time. Disable with NOTIFY_SWEEP_ENABLED=false.
async def _notification_sweep_loop():
    while True:
        await asyncio.sleep(NOTIFY_SWEEP_SECONDS)
        try:
            await asyncio.to_thread(notifications.run_sweep_once)
        except Exception as e:  # never let the loop die
            logging.getLogger(__name__).warning("notification sweep error: %s", e)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = None
    if os.getenv("NOTIFY_SWEEP_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off"):
        task = asyncio.create_task(_notification_sweep_loop())
    try:
        yield
    finally:
        if task:
            task.cancel()


app = FastAPI(lifespan=lifespan)

if not os.path.exists("static"):
    os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =====================================================================
# ALIAS ROUTE FOR LEGACY ANDROID APP VERSIONS
# This catches the old Android path and passes it to the new payments logic
# =====================================================================
@app.post("/ai/depthflow/verify-purchase", tags=["legacy-mobile"])
async def legacy_verify_google_play_purchase(
    request: GooglePlayPurchaseReq,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Acts as a bridge for older Android clients hitting the old endpoint.
    Passes the request cleanly to the payments_router function.
    """
    return await verify_google_play_purchase(request, current_user, db)


# --- HAPPY HOUR: structured errors  {"detail": "<message>", "code": "<STABLE_CODE>"} ---
@app.exception_handler(HappyHourError)
async def happy_hour_error_handler(request: Request, exc: HappyHourError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "code": exc.code})


# --- INCLUDE ALL STANDARD ROUTERS ---
app.include_router(auth_router.router)
app.include_router(depthflow_ai_router.router)
app.include_router(ai_router.router)
app.include_router(payments_router.router)
app.include_router(admin_router.router)
app.include_router(contact_router.router)
app.include_router(happy_hour_router.router)
app.include_router(notifications_router.router)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)