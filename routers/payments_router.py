import os
import logging
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from typing import Optional
from dodopayments import DodoPayments
from sqlalchemy.orm import Session
from standardwebhooks.webhooks import Webhook as StandardWebhook
from datetime import datetime
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

client = DodoPayments(
    bearer_token=DODO_API_KEY,
    environment="live_mode" 
)

# Configuration mapping
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

# --- 1. UNIFIED CHECKOUT & UPGRADE ENDPOINT ---
@router.post("/create-checkout-session")
async def create_checkout_session(
    request: CheckoutRequest, 
    current_user: User = Depends(get_current_user)
):
    """
    Handles both New Subscriptions (Checkout URL) and Plan Changes (Direct Update).
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
            
            client.subscriptions.change_plan(
                subscription_id=current_user.subscription_id,
                product_id=target_product_id,
                proration_billing_mode="prorated_immediately",
                quantity=request.quantity
            )
            
            # Return distinct response so Frontend knows NO redirect is needed
            return {
                "action": "updated",
                "message": f"Plan upgraded to {request.plan_name}. Changes will reflect shortly.",
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
        logger.error(f"Payment/Upgrade Error: {e}")
        raise HTTPException(status_code=400, detail=f"Payment Error: {str(e)}")


# --- 2. CANCEL SUBSCRIPTION ---
@router.post("/cancel-subscription")
async def cancel_subscription(
    current_user: User = Depends(get_current_user)
):
    """
    Schedules cancellation at the end of the billing cycle and returns the specific date.
    """
    if not current_user.subscription_id:
        raise HTTPException(status_code=400, detail="No active subscription found.")

    try:
        # 1. Update Dodo to stop auto-renewal
        # Capture the returned subscription object
        updated_sub = client.subscriptions.update(
            subscription_id=current_user.subscription_id,
            cancel_at_next_billing_date=True
        )
        
        # 2. Extract the end date
        # Note: adjust attribute name if Dodo returns 'next_payment_date' or similar
        end_date = updated_sub.next_billing_date 
        
        # Format the date nicely if it's a datetime object, otherwise use as string
        if isinstance(end_date, str):
            formatted_date = end_date[:10] # Take YYYY-MM-DD if it's a long ISO string
        else:
            formatted_date = str(end_date)

        # 3. Return the informative message
        return {
            "status": "success", 
            "message": f"Subscription has been scheduled for cancellation. You will retain access until {formatted_date}.",
            "end_date": formatted_date
        }

    except Exception as e:
        logger.error(f"Cancellation API Failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to cancel: {str(e)}")


# --- 3. WEBHOOK (THE SOURCE OF TRUTH) ---
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

        # --- HANDLER 1: NEW SUBSCRIPTION SUCCESS ---
        if event_type == "payment.succeeded":
            metadata = data.get("metadata", {})
            user_id = metadata.get("user_id")
            
            # User Lookup
            user = None
            if user_id:
                user = db.query(User).filter(User.id == int(user_id)).first()
            if not user: 
                 email = data.get("customer", {}).get("email")
                 if email: user = db.query(User).filter(User.email == email).first()

            if user:
                new_sub_id = data.get("subscription_id")
                is_renewal = user.subscription_id == new_sub_id
                
                # Logic: Credits
                credits_to_add = 0
                if "credits_to_add" in metadata:
                    # Case: New Checkout or Metadata preserved
                    credits_to_add = int(metadata.get("credits_to_add"))
                elif is_renewal:
                    # Case: Auto-renewal (metadata might be empty) -> Look up from Config
                    current_cycle = PLAN_CONFIG.get(user.billing_cycle, {})
                    current_plan = current_cycle.get(user.plan, {})
                    credits_to_add = current_plan.get("credits", 0)

                # Commit Updates
                if credits_to_add > 0:
                    user.credits += credits_to_add

                if new_sub_id:
                    user.subscription_id = new_sub_id
                    user.subscription_status = "active"
                
                # Apply Plan details from metadata if available (New Sub)
                if metadata.get("plan_name"): user.plan = metadata["plan_name"]
                if metadata.get("billing_cycle"): user.billing_cycle = metadata["billing_cycle"]

                db.commit()
                logger.info(f"DB Updated: Payment Success for {user.email}")


        # --- HANDLER 2: PLAN UPGRADE/DOWNGRADE SUCCESS ---
        elif event_type == "subscription.updated":
            # This fires when create_checkout_session calls client.subscriptions.update()
            sub_id = data.get("subscription_id")
            product_id = data.get("product_id")
            status = data.get("status")

            user = db.query(User).filter(User.subscription_id == sub_id).first()
            
            if user:
                user.subscription_status = status
                
                # Sync Plan Name from Product ID
                found_plan = False
                for cycle, plans in PLAN_CONFIG.items():
                    for p_name, p_data in plans.items():
                        if p_data["id"] == product_id:
                            user.plan = p_name
                            user.billing_cycle = cycle
                            
                            # Optional: Handle prorated credits for upgrades here if needed
                            # (Usually Dodo charges immediately -> triggers payment.succeeded -> adds credits there)
                            
                            found_plan = True
                            break
                    if found_plan: break
                
                db.commit()
                logger.info(f"DB Updated: Sub Updated for {user.email}")


        # --- HANDLER 3: CANCELLATION ---
        elif event_type == "subscription.cancelled":
            sub_id = data.get("subscription_id")
            user = db.query(User).filter(User.subscription_id == sub_id).first()
            
            if user:
                # Security Check: Ensure we aren't cancelling an old ID 
                # (e.g. user had ID_1, upgraded to ID_2, then ID_1 cancelled event arrived late)
                if user.subscription_id == sub_id:
                    user.subscription_status = "canceled"
                    user.plan = "Free"
                    user.subscription_id = None
                    user.billing_cycle = None
                    db.commit()
                    logger.info(f"DB Updated: Sub Cancelled for {user.email}")

        return {"status": "received"}

    except Exception as e:
        logger.error(f"Webhook Error: {e}")
        return {"status": "error", "detail": str(e)}