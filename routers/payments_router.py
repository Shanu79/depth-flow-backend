import os
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from dodopayments import DodoPayments
from standardwebhooks.webhooks import Webhook as StandardWebhook

from database import get_db
from models import User
from auth import get_current_user

# --- SETUP ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["payments"])

DODO_API_KEY = os.environ.get("DODO_PAYMENTS_API_KEY")
WEBHOOK_SECRET = os.environ.get("DODO_WEBHOOK_SECRET")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

# Initialize Dodo Client
client = DodoPayments(
    bearer_token=DODO_API_KEY,
    environment="live_mode" 
)

# Plan Configuration (Single Source of Truth)
PLAN_CONFIG = {
    "monthly": {
        "Basic": {"id": "pdt_0NUQxXnGKlkhrpGBAFMvy", "credits": 550},
        "Pro":   {"id": "pdt_0NUQxXJa0TR6vJJznsrt2", "credits": 1200},
        "Free":  {"id": "pdt_0NUQxWtv1A7PDo75MPx9L", "credits": 0},
    },
    "yearly": {
        "Basic": {"id": "pdt_0NUQxWNFaRWTd8eoNkqoJ", "credits": 6600},
        "Pro":   {"id": "pdt_0NUQxVyW088PIAwIzObuC", "credits": 14400},
        "Free":  {"id": "pdt_0NUQxWtv1A7PDo75MPx9L", "credits": 0},
    }
}

class CheckoutRequest(BaseModel):
    plan_name: str
    billing_cycle: str
    quantity: int = 1


# --- 1. UNIFIED CHECKOUT & UPGRADE/DOWNGRADE ---
@router.post("/create-checkout-session")
async def create_checkout_session(
    request: CheckoutRequest, 
    current_user: User = Depends(get_current_user)
):
    """
    Unified endpoint:
    - If user has NO active subscription -> Returns Checkout URL (New Sub).
    - If user HAS active subscription -> Upgrades/Downgrades instantly via API.
    """
    # 1. Validate Plan
    cycle_data = PLAN_CONFIG.get(request.billing_cycle)
    if not cycle_data:
        raise HTTPException(status_code=400, detail="Invalid billing cycle")

    plan_data = cycle_data.get(request.plan_name)
    if not plan_data:
        raise HTTPException(status_code=400, detail=f"Invalid plan: {request.plan_name}")

    target_product_id = plan_data["id"]
    credits_to_add = plan_data["credits"]

    try:
        # --- SCENARIO A: UPGRADE / DOWNGRADE (Active Subscription) ---
        if current_user.subscription_id and current_user.subscription_status == "active":
            
            # Use 'change_plan' for both Upgrades and Downgrades
            # This calculates the difference immediately.
            client.subscriptions.change_plan(
                subscription_id=current_user.subscription_id,
                product_id=target_product_id,
                proration_billing_mode="prorated_immediately", # Charges difference now
                quantity=request.quantity
            )
            
            # Return distinct response so Frontend knows NO redirect is needed
            return {
                "action": "updated",
                "message": f"Plan successfully changed to {request.plan_name}. Updates will reflect shortly.",
                "checkout_url": None
            }

        # --- SCENARIO B: NEW SUBSCRIPTION (No Active Sub) ---
        else:
            session = client.checkout_sessions.create(
                product_cart=[{
                    "product_id": target_product_id, 
                    "quantity": request.quantity
                }],
                customer={
                    "email": current_user.email,
                    "name": current_user.full_name or "User"
                },
                metadata={
                    "user_id": str(current_user.id),
                    "credits_to_add": str(credits_to_add),
                    "plan_name": request.plan_name,
                    "billing_cycle": request.billing_cycle
                },
                return_url=f"{FRONTEND_URL}/workspace", 
            )
            
            return {
                "action": "checkout",
                "checkout_url": session.checkout_url
            }

    except Exception as e:
        logger.error(f"Payment/Change Error: {e}")
        # Return Dodo's specific error message if possible
        raise HTTPException(status_code=400, detail=f"Payment Error: {str(e)}")


# --- 2. CANCEL SUBSCRIPTION ---
@router.post("/cancel-subscription")
async def cancel_subscription(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Schedules cancellation and updates DB status immediately.
    """
    if not current_user.subscription_id:
        raise HTTPException(status_code=400, detail="No active subscription found.")

    try:
        # 1. Update Dodo to stop auto-renewal
        updated_sub = client.subscriptions.update(
            subscription_id=current_user.subscription_id,
            cancel_at_next_billing_date=True
        )
        
        # 2. Extract End Date
        # Dodo usually returns 'next_billing_date' for the period end
        raw_date = getattr(updated_sub, 'next_billing_date', None)
        formatted_date = str(raw_date)[:10] if raw_date else datetime.now().strftime('%Y-%m-%d')

        # 3. UPDATE DATABASE STATUS IMMEDIATELY
        # As requested: "Scheduled for cancellation on [date]"
        new_status = f"Scheduled for cancellation on {formatted_date}"
        current_user.subscription_status = new_status
        db.commit()

        return {
            "status": "success", 
            "message": f"Subscription cancelled. Access remains until {formatted_date}.",
            "new_status": new_status
        }

    except Exception as e:
        logger.error(f"Cancellation API Failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to cancel: {str(e)}")

# --- 3. WEBHOOK (The Source of Truth) ---
@router.post("/webhook")
async def dodo_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        # 1. Verification
        payload_bytes = await request.body()
        payload_str = payload_bytes.decode("utf-8")
        headers = dict(request.headers)

        try:
            wh = StandardWebhook(WEBHOOK_SECRET)
            wh.verify(payload_str, headers)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid signature")

        # 2. Parse Event
        payload = await request.json()
        event_type = payload.get("type")
        data = payload.get("data", {})
        
        logger.info(f"Webhook Event: {event_type}")

        # --- A. PAYMENT SUCCEEDED (New Subs + Renewals) ---
        if event_type == "payment.succeeded":
            metadata = data.get("metadata", {})
            user_id = metadata.get("user_id")
            
            # User Lookup Strategy
            user = None
            if user_id:
                user = db.query(User).filter(User.id == int(user_id)).first()
            if not user: 
                 email = data.get("customer", {}).get("email")
                 if email: user = db.query(User).filter(User.email == email).first()

            if user:
                new_sub_id = data.get("subscription_id")
                is_renewal = user.subscription_id == new_sub_id
                
                # Credits Logic
                credits_to_add = 0
                if "credits_to_add" in metadata:
                    # Case 1: New Checkout (Metadata exists)
                    credits_to_add = int(metadata.get("credits_to_add"))
                elif is_renewal:
                    # Case 2: Auto-renewal (No metadata) -> Look up from Config based on CURRENT plan
                    current_cycle = PLAN_CONFIG.get(user.billing_cycle, {})
                    current_plan = current_cycle.get(user.plan, {})
                    credits_to_add = current_plan.get("credits", 0)

                # Update User
                if credits_to_add > 0:
                    user.credits += credits_to_add

                if new_sub_id:
                    user.subscription_id = new_sub_id
                    user.subscription_status = "active"
                
                # Update plan info if present (only on New/Upgrade checkouts)
                if metadata.get("plan_name"): user.plan = metadata["plan_name"]
                if metadata.get("billing_cycle"): user.billing_cycle = metadata["billing_cycle"]

                db.commit()
                logger.info(f"DB Updated: Payment Success for {user.email}")


        # --- B. SUBSCRIPTION UPDATED (Upgrades/Downgrades) ---
        elif event_type == "subscription.updated":
            sub_id = data.get("subscription_id")
            product_id = data.get("product_id")
            status = data.get("status")

            user = db.query(User).filter(User.subscription_id == sub_id).first()
            
            if user:
                # Update Status
                user.subscription_status = status
                
                # Sync Plan Name from Product ID (Reverse Lookup)
                found_plan = False
                for cycle, plans in PLAN_CONFIG.items():
                    for p_name, p_data in plans.items():
                        if p_data["id"] == product_id:
                            user.plan = p_name
                            user.billing_cycle = cycle
                            found_plan = True
                            break
                    if found_plan: break
                
                db.commit()
                logger.info(f"DB Updated: Plan Changed for {user.email}")


        # --- C. SUBSCRIPTION CANCELLED (Final Termination) ---
        elif event_type == "subscription.cancelled":
            sub_id = data.get("subscription_id")
            user = db.query(User).filter(User.subscription_id == sub_id).first()
            
            if user:
                # Race Condition Check:
                # Only cancel if the ID in the webhook matches the user's CURRENT ID.
                # This prevents cancelling a user who upgraded (got a new ID) but the old ID just expired.
                if user.subscription_id == sub_id:
                    user.subscription_status = "canceled"
                    user.plan = "Free"
                    user.subscription_id = None
                    user.billing_cycle = None
                    db.commit()
                    logger.info(f"DB Updated: Subscription Cancelled for {user.email}")

        return {"status": "received"}

    except Exception as e:
        logger.error(f"Webhook Error: {e}")
        return {"status": "error", "detail": str(e)}