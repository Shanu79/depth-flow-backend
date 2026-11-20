from datetime import datetime, timedelta
from database import SessionLocal
from models import User
import pricing

def run_monthly_drops():
    db = SessionLocal()
    now = datetime.utcnow()
    
    print(f"[{now}] Running Monthly Credit Drop Check...")
    
    try:
        users_due = db.query(User).filter(User.next_credit_drop_date <= now).all()
        
        for user in users_due:
            # FIX: Safely cast to lowercase to catch both your manual status and Dodo's webhook status
            plat_status = (user.subscription_status or "").lower()
            api_status = (user.api_subscription_status or "").lower()
            
            is_platform_active = "active" in plat_status or "scheduled" in plat_status
            is_api_active = "active" in api_status or "scheduled" in api_status
            
            if not is_platform_active and not is_api_active:
                user.next_credit_drop_date = None
                continue
                
            monthly_credits_to_add = 0
            
            if is_platform_active and user.billing_cycle == "yearly":
                yearly = pricing.yearly_credits(db, user.plan, api=False)
                if yearly:
                    monthly_credits_to_add += yearly // 12

            if is_api_active and user.api_billing_cycle == "yearly":
                yearly = pricing.yearly_credits(db, user.api_plan, api=True)
                if yearly:
                    monthly_credits_to_add += yearly // 12
                
            if monthly_credits_to_add > 0:
                user.credits = monthly_credits_to_add
                user.next_credit_drop_date = now + timedelta(days=30)
                print(f"✅ Dropped {monthly_credits_to_add} credits to {user.email}")
            else:
                user.next_credit_drop_date = None
                
        db.commit()
        print("✅ Cron job finished successfully.")
    except Exception as e:
        db.rollback()
        print(f"❌ Cron failed: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    run_monthly_drops()