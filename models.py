from sqlalchemy import Column, Integer, String, Boolean
from database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String, nullable=True) # Null if using Google
    full_name = Column(String, nullable=True)
    profile_pic = Column(String, nullable=True)
    provider = Column(String, default="local") # "google" or "local"
    
    # THE LEDGER
    credits = Column(Integer, default=20) # Give 20 free credits on signup