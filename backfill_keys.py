# backfill_keys.py
import secrets
from database import SessionLocal
from models import User

def generate_api_key():
    return f"df_{secrets.token_urlsafe(32)}"

def backfill_users():
    db = SessionLocal()
    try:
        users = db.query(User).filter(User.api_key == None).all()
        for user in users:
            user.api_key = generate_api_key()
            print(f"Generated key for {user.email}")
        
        db.commit()
        print(f"Successfully backfilled {len(users)} users.")
    except Exception as e:
        print(f"Error: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    backfill_users()