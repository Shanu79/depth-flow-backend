from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

import os
from dotenv import load_dotenv

load_dotenv()

# 1. Get the URL from environment variable, default to SQLite for local testing
# Note: Render/DigitalOcean use "postgres://", but SQLAlchemy needs "postgresql://"
database_url = os.getenv("DATABASE_URL", "sqlite:///./depthflow.db")
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

SQLALCHEMY_DATABASE_URL = database_url

# 2. Configure the engine
if "sqlite" in SQLALCHEMY_DATABASE_URL:
    # SQLite specific args
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
    )
else:
    # PostgreSQL (Production)
    engine = create_engine(SQLALCHEMY_DATABASE_URL)

# 3. Create a configured "Session" class
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()