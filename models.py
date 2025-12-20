from sqlalchemy import Column, Integer, String, Boolean, DateTime # <--- 1. Import DateTime
from database import Base
from datetime import datetime # <--- 2. Import datetime for default values if needed

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
    credits = Column(Integer, default=5) # Default to 5 or 20
    last_login_ip = Column(String, nullable=True)