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
            customer={"email": email}, 
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
    # STEP 0: STRICT "SINGLE ACTIVE PLAN" CHECK
    # ---------------------------------------------------------
    # If user has a subscription ID, verify its status.
    has_active_sub = False
    if current_user.subscription_id:
        # Check if actually active (or scheduled for cancel, which still counts as active access)
        status = current_user.subscription_status or ""
        if status == "active" or "Scheduled" in status:
            has_active_sub = True

    # CASE A: ONE-TIME PAYMENT (Always Allowed)
    if request.billing_cycle == "one_time":
        plan_data = ONE_TIME_PLANS.get(request.plan_name)
        if not plan_data:
            raise HTTPException(status_code=400, detail="Invalid one-time plan")
        
        target_product_id = plan_data["id"]
        is_one_time = True
        credits_to_add = plan_data["credits"]

        if request.plan_name == "Trial":
            if current_user.plan == "Trial":
                raise HTTPException(status_code=403, detail="You already have the Trial pack.")
            if check_if_already_purchased(current_user.email, target_product_id):
                raise HTTPException(status_code=403, detail="You have already used the Trial pack.")

    # CASE B: RECURRING SUBSCRIPTION
    else:
        # 1. PREVENT DOUBLE SUBSCRIPTION
        # If they have an active sub, they CANNOT create a new checkout session.
        # They must use the "Modify" flow (which happens below automatically if we detect they are just switching plans).
        # But if they are trying to buy a totally new sub while one exists, we block it.
        if has_active_sub:
            # Exception: They are trying to switch plans? 
            # We allow it ONLY if we are about to enter the "Modify Existing" block below.
            # If the code logic falls through to "New Checkout", we must block it.
            pass 

        cycle_data = PLAN_CONFIG.get(request.billing_cycle)
        if not cycle_data:
            raise HTTPException(status_code=400, detail="Invalid billing cycle")

        plan_data = cycle_data.get(request.plan_name)
        if not plan_data:
            raise HTTPException(status_code=400, detail=f"Invalid plan: {request.plan_name}")

        target_product_id = plan_data["id"]
        credits_to_add = plan_data["credits"]

    # ---------------------------------------------------------
    # STEP 2: EXECUTE PAYMENT OR MODIFICATION
    # ---------------------------------------------------------
    try:
        # --- SCENARIO A: MODIFY EXISTING SUBSCRIPTION ---
        # Only if: User has active sub AND this is NOT a one-time purchase
        if has_active_sub and not is_one_time:
            
            # Check if they are already on this plan
            if current_user.plan == request.plan_name and current_user.billing_cycle == request.billing_cycle:
                raise HTTPException(status_code=400, detail="You are already subscribed to this plan.")

            # 1. Reactivate if needed
            if "Scheduled for cancellation" in (current_user.subscription_status or ""):
                try:
                    client.subscriptions.update(
                        subscription_id=current_user.subscription_id,
                        cancel_at_next_billing_date=False 
                    )
                except Exception: pass 

            # 2. Change Plan via API
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
                    raise HTTPException(
                        status_code=409, 
                        detail="A previous payment is pending. Please wait for it to settle."
                    )
                else:
                    raise e 

            # 3. IMMEDIATE DB UPDATE
            current_user.plan = request.plan_name
            current_user.billing_cycle = request.billing_cycle
            current_user.subscription_status = "active"
            
            next_date = getattr(updated_sub, 'next_billing_date', None)
            formatted_date = str(next_date)[:10] if next_date else "next cycle"

            db.commit()
            
            return {
                "action": "updated",
                "message": f"Plan upgraded to {request.plan_name}. Next billing: {formatted_date}",
                "checkout_url": None
            }

        # --- SCENARIO B: NEW CHECKOUT (New Sub OR One-Time) ---
        else:
            # BLOCKER: If they have an active sub but logic reached here for a recurring plan, STOP.
            # This prevents creating a second subscription object in Dodo.
            if has_active_sub and not is_one_time:
                 raise HTTPException(
                    status_code=400, 
                    detail="You already have an active subscription. Manage it in settings instead of buying a new one."
                )

            metadata = {
                "user_id": str(current_user.id),
                "credits_to_add": str(credits_to_add),
                "plan_name": request.plan_name,
                "billing_cycle": request.billing_cycle,
                "is_one_time": "true" if is_one_time else "false"
            }

            session = client.checkout_sessions.create(
                product_cart=[{
                    "product_id": target_product_id, 
                    "quantity": request.quantity
                }],
                customer={
                    "email": current_user.email,
                    "name": current_user.full_name or "User"
                },
                metadata=metadata,
                return_url=f"{FRONTEND_URL}/workspace", 
            )
            return {"action": "checkout", "checkout_url": session.checkout_url}

    except HTTPException as he:
        raise he 
    except Exception as e:
        logger.error(f"Payment Error: {e}")
        raise HTTPException(status_code=400, detail=f"Payment Error: {str(e)}")


# --- 2. CANCEL SUBSCRIPTION ---
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

        return {
            "status": "success", 
            "message": f"Subscription cancelled. Access remains until {formatted_date}.",
            "new_status": new_status
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to cancel: {str(e)}")


# --- 3. WEBHOOK (UPDATED FOR SAFETY) ---
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

        # --- EVENT: PAYMENT SUCCEEDED ---
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
                
                # --- FIX: GET REAL PRODUCT ID ---
                # Check data structure for product_id. Sometimes it's in a cart list.
                purchased_product_id = None
                if "product_cart" in data and len(data["product_cart"]) > 0:
                    purchased_product_id = data["product_cart"][0].get("product_id")
                
                # Verify what plan this Product ID belongs to
                real_plan_name, real_cycle, real_credits, found_plan = get_plan_details(purchased_product_id)

                # --- DETERMINE CREDITS ---
                credits_to_add = 0
                
                if found_plan:
                    # TRUST THE CONFIG, NOT THE METADATA
                    credits_to_add = real_credits
                    logger.info(f"Verified Plan: {real_plan_name} (Credits: {credits_to_add})")
                else:
                    # Fallback (Should rarely happen if Config is in sync)
                    credits_to_add = int(metadata.get("credits_to_add", 0))
                    real_plan_name = metadata.get("plan_name", user.plan)
                    logger.warning("Product ID not found in config, falling back to metadata.")

                # --- ADD CREDITS ---
                if credits_to_add > 0:
                    user.credits += credits_to_add

                # --- SYNC STATE (PROTECTED) ---
                if is_one_time:
                    # For One-Time: Only update plan name if they don't have an active sub
                    if not user.subscription_id or user.subscription_status != "active":
                         user.plan = real_plan_name
                else:
                    # For Subscriptions: Update everything
                    if new_sub_id:
                        user.subscription_id = new_sub_id
                        user.subscription_status = "active"
                    
                    if found_plan:
                        user.plan = real_plan_name
                        user.billing_cycle = real_cycle

                db.commit()

        # --- EVENT: SUBSCRIPTION UPDATED ---
        elif event_type == "subscription.updated":
            sub_id = data.get("subscription_id")
            product_id = data.get("product_id")
            status = data.get("status")

            user = db.query(User).filter(User.subscription_id == sub_id).first()
            if user:
                user.subscription_status = status
                
                # Keep Plan in Sync
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

        # --- EVENT: SUBSCRIPTION CANCELLED ---
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

@router.post("/sync-subscription")
async def sync_subscription(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not current_user.subscription_id:
        return {"status": "ignored", "message": "No active subscription to sync."}

    user_db = db.query(User).filter(User.id == current_user.id).first()
    if not user_db:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        dodo_sub = client.subscriptions.retrieve(subscription_id=user_db.subscription_id)
        real_status = dodo_sub.status 
        real_product_id = dodo_sub.product_id
        
        is_scheduled_cancel = getattr(dodo_sub, 'cancel_at_next_billing_date', False)
                              
        if real_status in ["cancelled", "expired", "past_due"]:
             if user_db.credits > 0: user_db.credits = 0
             user_db.plan = "Free"
             user_db.subscription_id = None
             final_status = "cancelled"
        elif real_status == "active" and is_scheduled_cancel:
            next_date = getattr(dodo_sub, 'next_billing_date', None)
            fmt_date = str(next_date)[:10] if next_date else "soon"
            final_status = f"Scheduled for cancellation on {fmt_date}"
        else:
            final_status = real_status

        user_db.subscription_status = final_status

        # Plan Sync
        found_plan = False
        for cycle, plans in PLAN_CONFIG.items():
            for p_name, p_data in plans.items():
                if str(p_data["id"]) == str(real_product_id):
                    user_db.plan = p_name
                    user_db.billing_cycle = cycle
                    found_plan = True
                    break
            if found_plan: break
        
        db.add(user_db)
        db.commit()
        db.refresh(user_db)

        return {"status": "success", "synced_plan": user_db.plan}

    except Exception as e:
        if "404" in str(e):
             user_db.subscription_status = "canceled"
             user_db.subscription_id = None
             db.commit()
             return {"status": "success", "message": "Subscription was deleted remotely."}
        raise HTTPException(status_code=500, detail="Failed to sync subscription.")