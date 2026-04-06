from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from database import Base
from datetime import datetime

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String, nullable=True)
    full_name = Column(String, nullable=True)
    profile_pic = Column(String, nullable=True)
    provider = Column(String, default="local")
    
    # --- WORKSPACE / PLATFORM SUBSCRIPTION ---
    plan = Column(String, default="free") 
    billing_cycle = Column(String, nullable=True) 
    subscription_id = Column(String, nullable=True) 
    subscription_status = Column(String, default="inactive") 
    subscription_end_date = Column(DateTime, nullable=True) 
    
    # --- API SUBSCRIPTION (NEW FIELDS) ---
    api_plan = Column(String, default="free") # "Free", "Api-Pro", etc.
    api_billing_cycle = Column(String, nullable=True) 
    api_subscription_id = Column(String, nullable=True) 
    api_subscription_status = Column(String, default="inactive") 
    api_subscription_end_date = Column(DateTime, nullable=True) 
    
    # THE LEDGER (Shared between platform and API)
    credits = Column(Integer, default=20)
    last_login_ip = Column(String, nullable=True)
    is_admin = Column(Boolean, default=False)
    
    api_key = Column(String, unique=True, index=True, nullable=True)
    
    # Inside your User class in models.py add:
    is_verified = Column(Boolean, default=False)
    otp = Column(String, nullable=True)
    otp_expiry = Column(DateTime, nullable=True)
class GenerationHistory(Base):
    __tablename__ = "generation_history"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    video_url = Column(String, nullable=False)
    # We calculate expiry based on created_at + 30 mins
    created_at = Column(DateTime, default=datetime.utcnow) 
    
    source = Column(String, default="workspace")
    # Relationship to user
    owner = relationship("User", back_populates="generations")

# Update User class to include the relationship
User.generations = relationship("GenerationHistory", back_populates="owner")