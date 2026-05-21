# cron_monthly_credits.py
from datetime import datetime, timedelta
from database import SessionLocal
from models import User
from routers.payments_router import PLAN_CONFIG, API_PLAN_CONFIG

def run_monthly_drops():
    db = SessionLocal()
    now = datetime.utcnow()
    
    print(f"[{now}] Running Monthly Credit Drop Check...")
    
    try:
        # Find all users whose drop date is today or in the past
        users_due = db.query(User).filter(User.next_credit_drop_date <= now).all()
        
        for user in users_due:
            # Ensure they are actually active
            is_platform_active = user.subscription_status and ("active" in user.subscription_status or "Scheduled" in user.subscription_status)
            is_api_active = user.api_subscription_status and ("active" in user.api_subscription_status or "Scheduled" in user.api_subscription_status)
            
            if not is_platform_active and not is_api_active:
                # If both are inactive, clean up the date and skip
                user.next_credit_drop_date = None
                continue
                
            monthly_credits_to_add = 0
            
            # Check Platform Yearly Plan
            if is_platform_active and user.billing_cycle == "yearly" and user.plan in PLAN_CONFIG["yearly"]:
                monthly_credits_to_add += PLAN_CONFIG["yearly"][user.plan]["credits"] // 12
                
            # Check API Yearly Plan
            if is_api_active and user.api_billing_cycle == "yearly" and user.api_plan in API_PLAN_CONFIG["yearly"]:
                monthly_credits_to_add += API_PLAN_CONFIG["yearly"][user.api_plan]["credits"] // 12
                
            if monthly_credits_to_add > 0:
                # Reset credits to their monthly allowance (Use-it-or-lose-it model)
                user.credits = monthly_credits_to_add
                
                # Push the next drop date forward by 30 days
                user.next_credit_drop_date = now + timedelta(days=30)
                print(f"✅ Dropped {monthly_credits_to_add} credits to {user.email}")
            else:
                # If they aren't on a yearly plan anymore, remove the tracker
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