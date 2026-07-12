import os
from pathlib import Path
import uvicorn
from fastapi import FastAPI, Depends  # Added Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from database import engine, Base, get_db  # Added get_db

# --- IMPORT THE ROUTERS ---
from routers import auth_router, depthflow_ai_router, payments_router, admin_router, ai_router, contact_router

# --- IMPORTS FOR THE ALIAS ROUTE ---
# Adjust these imports if your folder structure differs slightly
from routers.payments_router import verify_google_play_purchase, GooglePlayPurchaseReq
from models import User
from auth import get_current_user
from sqlalchemy.orm import Session

# 1. LOAD ENV VARS FIRST
load_dotenv()

# Create DB Tables
Base.metadata.create_all(bind=engine)

app = FastAPI()

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


# --- INCLUDE ALL STANDARD ROUTERS ---
app.include_router(auth_router.router)
app.include_router(depthflow_ai_router.router)
app.include_router(ai_router.router)
app.include_router(payments_router.router)
app.include_router(admin_router.router)
app.include_router(contact_router.router)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)