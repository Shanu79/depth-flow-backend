import os
from datetime import datetime, timedelta
from jose import jwt, JWTError
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, APIKeyHeader
from sqlalchemy.orm import Session
from passlib.context import CryptContext 
from database import get_db
from models import User

SECRET_KEY = os.getenv("APP_SECRET_KEY", "change_this_secret_in_prod")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7 

# --- PASSWORD HASHING SETUP ---
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

# auto_error=False prevents FastAPI from automatically rejecting requests without a token, 
# allowing us to check for an API key instead.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False) 
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)
# ------------------------------

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# --- HYBRID AUTHENTICATION ---
async def get_current_user(
    token: str = Depends(oauth2_scheme), 
    api_key: str = Depends(api_key_header),
    db: Session = Depends(get_db)
):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    # 1. Check API Key First (For Developer / Machine Access)
    if api_key:
        user = db.query(User).filter(User.api_key == api_key).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API Key")
        return user

    # 2. Fallback to JWT Token (For Frontend Web App Access)
    if token:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            email: str = payload.get("sub")
            if email is None:
                raise credentials_exception
        except JWTError:
            raise credentials_exception
            
        user = db.query(User).filter(User.email == email).first()
        if user is None:
            raise credentials_exception
        return user

    # 3. If neither is provided, reject the request
    raise credentials_exception