# main.py
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from database import engine, Base

# --- IMPORT THE NEW ROUTER HERE ---
from routers import auth_router, depthflow_ai_router, payments_router, admin_router, ai_router, contact_router
from dotenv import load_dotenv

# 1. LOAD ENV VARS FIRST
load_dotenv()

import os
from pathlib import Path
from fastapi.staticfiles import StaticFiles

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

# --- INCLUDE THE NEW ROUTER HERE ---
app.include_router(auth_router.router)
app.include_router(depthflow_ai_router.router)
app.include_router(ai_router.router)
app.include_router(payments_router.router)
app.include_router(admin_router.router)
app.include_router(contact_router.router)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)