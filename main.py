import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from database import engine, Base
from routers import auth_router, ai_router
from dotenv import load_dotenv
import os
from pathlib import Path

# Load environment variables from the .env file
load_dotenv()


# Create DB Tables
Base.metadata.create_all(bind=engine)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(ai_router.router)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)