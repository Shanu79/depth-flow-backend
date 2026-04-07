from database import SessionLocal
from models import User
from auth import create_access_token

def create_dummy_user():
    db = SessionLocal()
    test_email = "dummy_tester@example.com"
    
    # 1. Check if the dummy user already exists
    user = db.query(User).filter(User.email == test_email).first()
    
    # 2. If not, create them!
    if not user:
        user = User(
            email=test_email,
            full_name="Dummy Tester",
            plan="Free",
            credits=0,
            is_verified=True, # Bypass OTP check
            api_key="df_test_dummy_key_123"
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        print("✅ Created new Dummy User in database!")
    else:
        print("ℹ️ Dummy User already exists in database.")

    # 3. Generate a valid JWT Token using your app's secret key
    token = create_access_token(data={"sub": user.email})
    
    print("\n" + "="*50)
    print("🎯 YOUR DUMMY ACCESS TOKEN:")
    print("="*50)
    print(token)
    print("="*50 + "\n")
    print("Copy the token above and paste it into ACCESS_TOKEN in test_payments.py")
    
    db.close()

if __name__ == "__main__":
    create_dummy_user()