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
    
    # --- SUBSCRIPTION TRACKING ---
    plan = Column(String, default="free") # "Free", "Basic", "Pro"
    billing_cycle = Column(String, nullable=True) # "monthly" or "yearly"
    
    # Critical for managing access:
    subscription_id = Column(String, nullable=True) # The ID from Dodo Payments
    subscription_status = Column(String, default="inactive") # "active", "cancelled", "past_due"
    subscription_end_date = Column(DateTime, nullable=True) # When does the plan expire?
    
    # THE LEDGER
    credits = Column(Integer, default=0)
    last_login_ip = Column(String, nullable=True)
    is_admin = Column(Boolean, default=False)
    
    api_key = Column(String, unique=True, index=True, nullable=True)
    
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