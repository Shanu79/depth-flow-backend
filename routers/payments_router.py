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

client = DodoPayments(
    bearer_token=DODO_API_KEY,
    environment="live_mode" 
)

# SINGLE SOURCE OF TRUTH FOR CREDITS
PLAN_CONFIG = {
    "monthly": {
        "Basic": {"id": "pdt_0NUQxXnGKlkhrpGBAFMvy", "credits": 550},
        "Pro":   {"id": "pdt_0NUQxXJa0TR6vJJznsrt2", "credits": 1200},
    },
    "yearly": {
        "Basic": {"id": "pdt_0NUQxWNFaRWTd8eoNkqoJ", "credits": 6600},
        "Pro":   {"id": "pdt_0NUQxVyW088PIAwIzObuC", "credits": 14400},
    }
}

ONE_TIME_PLANS = {
    "Trial": {"id": "pdt_0NUQxWtv1A7PDo75MPx9L", "credits": 120}
}

class CheckoutRequest(BaseModel):
    plan_name: str
    billing_cycle: str
    quantity: int = 1

# --- HELPER: GET PLAN DETAILS FROM PRODUCT ID ---
def get_plan_details(product_id: str):
    """
    Reverse lookup: Finds the Plan Name and Cycle based on the Dodo Product ID.
    Returns: (plan_name, billing_cycle, credits, is_found)
    """
    if not product_id:
        return None, None, 0, False

    # Check Recurring Plans
    for cycle, plans in PLAN_CONFIG.items():
        for p_name, p_data in plans.items():
            if p_data["id"] == product_id:
                return p_name, cycle, p_data["credits"], True
    
    # Check One-Time Plans
    for p_name, p_data in ONE_TIME_PLANS.items():
        if p_data["id"] == product_id:
            return p_name, "one_time", p_data["credits"], True
            
    return None, None, 0, False

def check_if_already_purchased(email: str, product_id: str) -> bool:
    try:
        payments = client.payments.list(
            customer_id={"email": email}, 
            limit=100
        )
        for payment in payments.items: 
            if (payment.product_id == product_id and payment.status == "succeeded"):
                return True
        return False
    except Exception as e:
        logger.error(f"Failed to check payment history: {e}")
        return False

# --- 1. UNIFIED CHECKOUT & UPGRADE/DOWNGRADE ---
@router.post("/create-checkout-session")
async def create_checkout_session(
    request: CheckoutRequest, 
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    target_product_id = None
    credits_to_add = 0
    is_one_time = False

    # ---------------------------------------------------------
    # STEP 1: VALIDATE PLAN (One-Time vs Recurring)
    # ---------------------------------------------------------
    
    # CASE A: ONE-TIME PAYMENT
    if request.billing_cycle == "one_time":
        plan_data = ONE_TIME_PLANS.get(request.plan_name)
        if not plan_data:
            raise HTTPException(status_code=400, detail="Invalid one-time plan")
        
        target_product_id = plan_data["id"]
        is_one_time = True
        credits_to_add = plan_data["credits"]

        # --- HISTORY CHECK ---
        if request.plan_name == "Trial":
            if current_user.plan == "Trial":
                raise HTTPException(status_code=403, detail="You already have the Trial pack.")
            if check_if_already_purchased(current_user.email, target_product_id):
                raise HTTPException(status_code=403, detail="You have already used the Trial pack.")
        
    # CASE B: RECURRING SUBSCRIPTION
    else:
        cycle_data = PLAN_CONFIG.get(request.billing_cycle)
        if not cycle_data:
            raise HTTPException(status_code=400, detail="Invalid billing cycle")

        plan_data = cycle_data.get(request.plan_name)
        if not plan_data:
            raise HTTPException(status_code=400, detail=f"Invalid plan: {request.plan_name}")

        target_product_id = plan_data["id"]
        credits_to_add = plan_data["credits"]

    # ---------------------------------------------------------
    # STEP 2: EXECUTE PAYMENT
    # ---------------------------------------------------------
    current_status = current_user.subscription_status or ""
    
    # Robust check for active subscription
    has_active_sub = current_user.subscription_id and (
        current_status == "active" or "Scheduled" in current_status
    )

    try:
        # --- SCENARIO A: MODIFY EXISTING SUBSCRIPTION ---
        if has_active_sub and not is_one_time:
            
            # Check if they are already on this exact plan/cycle
            if current_user.plan == request.plan_name and current_user.billing_cycle == request.billing_cycle:
                 raise HTTPException(status_code=400, detail="You are already on this plan.")

            # 1. Reactivate if needed
            if "Scheduled for cancellation" in current_status:
                try:
                    client.subscriptions.update(
                        subscription_id=current_user.subscription_id,
                        cancel_at_next_billing_date=False 
                    )
                except Exception: pass 

            # 2. Change Plan
            try:
                updated_sub = client.subscriptions.change_plan(
                    subscription_id=current_user.subscription_id,
                    product_id=target_product_id,
                    proration_billing_mode="prorated_immediately",
                    quantity=request.quantity
                )
            except Exception as e:
                error_str = str(e)
                if "409" in error_str:
                    raise HTTPException(status_code=409, detail="Pending payment processing. Please wait.")
                else:
                    raise e 

            # 3. DB UPDATE
            current_user.plan = request.plan_name
            current_user.billing_cycle = request.billing_cycle
            current_user.subscription_status = "active"
            
            next_date = getattr(updated_sub, 'next_billing_date', None)
            formatted_date = str(next_date)[:10] if next_date else "next billing cycle"

            db.commit()
            
            return {
                "action": "updated",
                "message": f"Plan upgraded to {request.plan_name}. Active now.",
                "checkout_url": None
            }

        # --- SCENARIO B: NEW CHECKOUT ---
        else:
            # GUARD: Block if they have active sub but tried to buy a new recurring plan
            if has_active_sub and not is_one_time:
                 raise HTTPException(
                    status_code=400, 
                    detail="You already have an active subscription. Please manage it from your settings."
                )
            
            metadata = {
                "user_id": str(current_user.id),
                "credits_to_add": str(credits_to_add),
                "plan_name": request.plan_name,
                "billing_cycle": request.billing_cycle,
                "is_one_time": "true" if is_one_time else "false"
            }

            session = client.checkout_sessions.create(
                product_cart=[{"product_id": target_product_id, "quantity": request.quantity}],
                customer={"email": current_user.email, "name": current_user.full_name or "User"},
                metadata=metadata,
                return_url=f"{FRONTEND_URL}/workspace", 
            )
            return {"action": "checkout", "checkout_url": session.checkout_url}

    except HTTPException as he:
        raise he 
    except Exception as e:
        logger.error(f"Payment Error: {e}")
        raise HTTPException(status_code=400, detail=f"Payment Error: {str(e)}")

# --- 2. CANCEL SUBSCRIPTION (Unchanged) ---
@router.post("/cancel-subscription")
async def cancel_subscription(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not current_user.subscription_id:
        raise HTTPException(status_code=400, detail="No active subscription found.")

    try:
        updated_sub = client.subscriptions.update(
            subscription_id=current_user.subscription_id,
            cancel_at_next_billing_date=True
        )
        
        raw_date = getattr(updated_sub, 'next_billing_date', None)
        formatted_date = str(raw_date)[:10] if raw_date else datetime.now().strftime('%Y-%m-%d')

        new_status = f"Scheduled for cancellation on {formatted_date}"
        current_user.subscription_status = new_status
        db.commit()

        return {"status": "success", "message": f"Subscription cancelled. Ends {formatted_date}.", "new_status": new_status}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to cancel: {str(e)}")


# --- 3. WEBHOOK (Robust) ---
@router.post("/webhook")
async def dodo_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        payload_bytes = await request.body()
        payload_str = payload_bytes.decode("utf-8")
        headers = dict(request.headers)

        try:
            wh = StandardWebhook(WEBHOOK_SECRET)
            wh.verify(payload_str, headers)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid signature")

        payload = await request.json()
        event_type = payload.get("type")
        data = payload.get("data", {})
        
        logger.info(f"Webhook Event: {event_type}")

        if event_type == "payment.succeeded":
            metadata = data.get("metadata", {})
            user_id = metadata.get("user_id")
            
            # Identify User
            user = None
            if user_id:
                user = db.query(User).filter(User.id == int(user_id)).first()
            if not user: 
                 email = data.get("customer", {}).get("email")
                 if email: user = db.query(User).filter(User.email == email).first()

            if user:
                new_sub_id = data.get("subscription_id")
                is_one_time = metadata.get("is_one_time") == "true"
                
                # --- ROBUST PRODUCT ID EXTRACTION ---
                # Checks 3 different locations for the Product ID to be safe
                purchased_product_id = None
                
                # 1. Try Product Cart
                if "product_cart" in data and isinstance(data["product_cart"], list) and len(data["product_cart"]) > 0:
                    purchased_product_id = data["product_cart"][0].get("product_id")
                
                # 2. Try Root Level
                if not purchased_product_id:
                    purchased_product_id = data.get("product_id")

                # 3. Try Lines (Stripe style)
                if not purchased_product_id and "lines" in data:
                     lines = data.get("lines", {}).get("data", [])
                     if lines: purchased_product_id = lines[0].get("price", {}).get("product")

                logger.info(f"Extracted Product ID: {purchased_product_id}")

                # --- VERIFY PLAN ---
                real_plan_name, real_cycle, real_credits, found_plan = get_plan_details(purchased_product_id)

                # --- DETERMINE CREDITS ---
                credits_to_add = 0
                
                if found_plan:
                    credits_to_add = real_credits
                    logger.info(f"Verified Plan via Config: {real_plan_name}")
                else:
                    # FALLBACK to Metadata
                    logger.warning(f"Product ID {purchased_product_id} not in config. Using metadata fallback.")
                    credits_to_add = int(metadata.get("credits_to_add", 0))
                    real_plan_name = metadata.get("plan_name", user.plan)
                    real_cycle = metadata.get("billing_cycle", user.billing_cycle)

                # --- UPDATE DB ---
                if credits_to_add > 0:
                    user.credits += credits_to_add
                    logger.info(f"Added {credits_to_add} credits to User {user.id}")

                if is_one_time:
                    if not user.subscription_id or user.subscription_status != "active":
                         user.plan = real_plan_name
                else:
                    if new_sub_id:
                        user.subscription_id = new_sub_id
                        user.subscription_status = "active"
                    if real_plan_name: user.plan = real_plan_name
                    if real_cycle: user.billing_cycle = real_cycle
                    
                db.commit()

        # --- OTHER EVENTS (Updates, Cancellations) ---
        elif event_type == "subscription.updated":
            # ... (Same as your code) ...
            sub_id = data.get("subscription_id")
            product_id = data.get("product_id")
            status = data.get("status")
            user = db.query(User).filter(User.subscription_id == sub_id).first()
            if user:
                user.subscription_status = status
                _, _, _, found = get_plan_details(product_id)
                # Only update plan info if we found the product ID in our config
                if found:
                    p_name, cycle, _, _ = get_plan_details(product_id)
                    user.plan = p_name
                    user.billing_cycle = cycle
                db.commit()

        elif event_type == "subscription.cancelled":
            sub_id = data.get("subscription_id")
            user = db.query(User).filter(User.subscription_id == sub_id).first()
            if user:
                user.subscription_status = "cancelled"
                user.plan = "Free"
                user.credits = 0 
                user.subscription_id = None
                user.billing_cycle = None
                db.commit()

        return {"status": "received"}

    except Exception as e:
        logger.error(f"Webhook Error: {e}")
        return {"status": "error", "detail": str(e)}
    
# --- SYNC (Unchanged) ---
@router.post("/sync-subscription")
async def sync_subscription(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # ... (Your existing sync logic is fine, just ensure it handles the db fetch) ...
    if not current_user.subscription_id:
        return {"status": "ignored"}
    
    user_db = db.query(User).filter(User.id == current_user.id).first()
    if not user_db: raise HTTPException(status_code=404)

    try:
        dodo_sub = client.subscriptions.retrieve(subscription_id=user_db.subscription_id)
        # ... (rest of your sync logic) ...
        # Copied for completeness of the file structure
        real_status = dodo_sub.status 
        is_scheduled_cancel = getattr(dodo_sub, 'cancel_at_next_billing_date', False)
        
        if real_status in ["cancelled", "expired", "past_due"]:
             if user_db.credits > 0: user_db.credits = 0
             user_db.plan = "Free"
             user_db.subscription_id = None
             final_status = "cancelled"
        elif real_status == "active" and is_scheduled_cancel:
            final_status = "Scheduled for cancellation"
        else:
            final_status = real_status

        user_db.subscription_status = final_status
        db.commit()
        return {"status": "success", "synced_plan": user_db.plan}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))