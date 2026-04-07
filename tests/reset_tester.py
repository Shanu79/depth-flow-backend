from database import SessionLocal
from models import User

def reset_dummy_user():
    db = SessionLocal()
    test_email = "dummy_tester@example.com"
    
    # 1. Find the test user
    user = db.query(User).filter(User.email == test_email).first()
    
    if user:
        # 2. Reset all billing and credit fields
        user.credits = 0
        user.plan = "Free"
        user.billing_cycle = None
        user.subscription_id = None
        user.subscription_status = None
        
        user.api_plan = "Free"
        user.api_billing_cycle = None
        user.api_subscription_id = None
        user.api_subscription_status = None
        
        # 3. Save changes
        db.commit()
        print(f"✅ Successfully reset {test_email}!")
        print("-> Credits: 0")
        print("-> Plan: Free")
    else:
        print(f"⚠️ User {test_email} not found in the database.")

    db.close()

if __name__ == "__main__":
    reset_dummy_user()