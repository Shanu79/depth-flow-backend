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
    "Trial": {"id": "pdt_0NUQxWtv1A7PDo75MPx9L", "credits": 120},
    "Credits Pack": {"id": "pdt_0NYCtBgPqWWzyF6yKrE98", "credits": 750},
}

class CheckoutRequest(BaseModel):
    plan_name: str
    billing_cycle: str
    quantity: int = 1

# Safer version if the dictionary approach fails
def check_if_already_purchased(email: str, product_id: str) -> bool:
    try:
        # 1. Get Customer ID first
        customers = client.customers.list(email=email, limit=1)
        if not customers.items:
            return False # Customer doesn't exist, so they haven't bought anything
        
        customer_id = customers.items[0].customer_id

        # 2. List payments using the ID string
        payments = client.payments.list(customer_id=customer_id, limit=100)

        for payment in payments.items: 
            if payment.product_id == product_id and payment.status == "succeeded":
                return True
        return False
    except Exception as e:
        logger.error(f"History check failed: {e}")
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

    # Define active status helper early for reuse
    current_status = current_user.subscription_status or ""
    has_active_sub = current_user.subscription_id and (
        current_status == "active" or "Scheduled for cancellation" in current_status
    )

    # ---------------------------------------------------------
    # STEP 1: VALIDATE PLAN (One-Time vs Recurring)
    # ---------------------------------------------------------
    
    # CASE A: ONE-TIME PAYMENT
    if request.billing_cycle == "one_time":
        plan_data = ONE_TIME_PLANS.get(request.plan_name)
        if not plan_data:
            raise HTTPException(status_code=400, detail="Invalid one-time plan")
        
        target_product_id = plan_data["id"]

        # --- LOGIC A.1: HISTORY CHECK (Trial) ---
        if request.plan_name == "Trial":
            if current_user.plan == "Trial":
                raise HTTPException(status_code=403, detail="You already have the Trial pack.")
            
            already_bought = check_if_already_purchased(current_user.email, target_product_id)
            if already_bought:
                raise HTTPException(status_code=403, detail="You have already used the Trial pack in the past.")

        # --- LOGIC A.2: SUBSCRIPTION CHECK (Credit Pack) ---
        # User must have an active subscription to buy add-on credits
        if request.plan_name == "Credits Pack":
            if not has_active_sub:
                raise HTTPException(
                    status_code=403, 
                    detail="You must have an active subscription (Basic or Pro) to purchase add-on credit packs."
                )

        credits_to_add = plan_data["credits"]
        is_one_time = True

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
    
    try:
        # --- SCENARIO A: MODIFY EXISTING SUBSCRIPTION ---
        # Only if user has active sub AND this is NOT a one-time purchase
        if has_active_sub and not is_one_time:
            
            # 1. Reactivate if needed
            if "Scheduled for cancellation" in current_status:
                try:
                    client.subscriptions.update(
                        subscription_id=current_user.subscription_id,
                        cancel_at_next_billing_date=False 
                    )
                except Exception:
                    pass 

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
                if "PREVIOUS_PAYMENT_PENDING" in error_str or "409" in error_str:
                    logger.warning(f"409 Error for user {current_user.id}: Pending Payment")
                    raise HTTPException(
                        status_code=409, 
                        detail="A previous payment is currently processing. Please wait a few minutes for it to settle before changing plans."
                    )
                else:
                    raise e 

            # 3. IMMEDIATE DB UPDATE
            current_user.plan = request.plan_name
            current_user.billing_cycle = request.billing_cycle
            current_user.subscription_status = "active"
            
            next_date = getattr(updated_sub, 'next_billing_date', None)
            formatted_date = str(next_date)[:10] if next_date else "next billing cycle"

            db.commit()
            
            return {
                "action": "updated",
                "message": f"Plan upgraded to {request.plan_name}. Active now. Next billing: {formatted_date}",
                "checkout_url": None
            }

        # --- SCENARIO B: NEW CHECKOUT (New Sub OR One-Time) ---
        else:
            # We construct metadata to handle both cases
            metadata = {
                "user_id": str(current_user.id),
                "credits_to_add": str(credits_to_add),
                "plan_name": request.plan_name,
                "billing_cycle": request.billing_cycle,
                "is_one_time": "true" if is_one_time else "false"
            }

            logger.info(f"🛒 GENERATING CHECKOUT FOR: {target_product_id}")
            
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
        logger.error(f"Payment/Change Error: {e}")
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
        logger.error(f"Cancellation API Failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to cancel: {str(e)}")


# --- 3. WEBHOOK (Handles Credits for ALL Payments) ---
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
            
            # 1. Identify User
            user = None
            if user_id:
                user = db.query(User).filter(User.id == int(user_id)).first()
            if not user: 
                 email = data.get("customer", {}).get("email")
                 if email: user = db.query(User).filter(User.email == email).first()

            if user:
                new_sub_id = data.get("subscription_id")
                is_renewal = user.subscription_id == new_sub_id
                
                # --- 1. CHECK ONE-TIME FLAG ---
                # We interpret the string "true" from metadata
                is_one_time = metadata.get("is_one_time") == "true"

                # --- 2. DETERMINE CREDITS ---
                credits_to_add = 0
                source = "Unknown"

                if "credits_to_add" in metadata:
                    # Case A: New Subscription OR One-Time Pack
                    credits_to_add = int(metadata.get("credits_to_add"))
                    source = "Metadata (New/One-Time)"
                elif is_renewal:
                    # Case B: Auto-Renewal (Recurring)
                    current_cycle = PLAN_CONFIG.get(user.billing_cycle, {})
                    current_plan = current_cycle.get(user.plan, {})
                    credits_to_add = current_plan.get("credits", 0)
                    source = f"Auto-Renewal ({user.plan})"

                # --- 3. ADD CREDITS ---
                if credits_to_add > 0:
                    user.credits += credits_to_add # Use += so we don't reset existing credits
                    logger.info(f"ADDED {credits_to_add} CREDITS to User {user.email}. Source: {source}")

                # --- 4. SYNC STATE (PROTECTED) ---
                if is_one_time:
                    # FOR ONE-TIME PAYMENTS:
                    # We ONLY update the plan name if the user has NO active subscription.
                    # This records that they bought the Trial, but protects "Pro" users from being overwritten.
                    if not user.subscription_id or user.subscription_status != "active":
                         user.plan = metadata.get("plan_name", user.plan)
                    
                    # We DO NOT update subscription_id or billing_cycle for one-time payments.
                else:
                    # FOR SUBSCRIPTIONS:
                    if new_sub_id:
                        user.subscription_id = new_sub_id
                        user.subscription_status = "active"
                    
                    if metadata.get("plan_name"): user.plan = metadata["plan_name"]
                    if metadata.get("billing_cycle"): user.billing_cycle = metadata["billing_cycle"]

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
            
            if user and user.subscription_id == sub_id:
                logger.info(f"Subscription expired for User {user.email}. Zeroing credits.")
                
                # 1. Update Status
                user.subscription_status = "cancelled"
                user.plan = "Free"
                
                # 2. ZERO OUT CREDITS (The Requirement)
                user.credits = 0 
                
                # 3. Cleanup IDs
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
    """
    Force-syncs local DB with Dodo.
    Uses explicit DB re-fetching to ensure updates persist.
    """
    if not current_user.subscription_id:
        return {"status": "ignored", "message": "No active subscription to sync."}

    # 1. CRITICAL FIX: Re-fetch user from the current DB session
    # This ensures we are modifying the actual DB row, not a copy.
    user_db = db.query(User).filter(User.id == current_user.id).first()
    
    if not user_db:
        raise HTTPException(status_code=404, detail="User not found in DB")

    try:
        # 2. Fetch REAL status from Dodo
        dodo_sub = client.subscriptions.retrieve(
            subscription_id=user_db.subscription_id
        )

        # 3. Determine Status
        real_status = dodo_sub.status 
        real_product_id = dodo_sub.product_id
        
        # Check for scheduled cancellation
        is_scheduled_cancel = getattr(dodo_sub, 'cancel_at_next_billing_date', False) or \
                              getattr(dodo_sub, 'cancel_at_period_end', False)
                              
        if real_status in ["cancelled", "expired", "past_due"]:
             if user_db.credits > 0:
                 logger.info(f"Sync detected expired plan. Zeroing {user_db.credits} credits.")
                 user_db.credits = 0
             
             user_db.plan = "Free"
             user_db.subscription_id = None
             user_db.billing_cycle = None
             final_status = "cancelled"

        if real_status == "active" and is_scheduled_cancel:
            next_date = getattr(dodo_sub, 'next_billing_date', None)
            fmt_date = str(next_date)[:10] if next_date else "soon"
            final_status = f"Scheduled for cancellation on {fmt_date}"
        else:
            final_status = real_status

        # 4. Apply Status Update
        if user_db.subscription_status != final_status:
             user_db.subscription_status = final_status

        # 5. Apply Plan Update (Reverse Lookup)
        found_plan = False
        for cycle, plans in PLAN_CONFIG.items():
            for p_name, p_data in plans.items():
                # Compare IDs safely (strip whitespace just in case)
                config_id = str(p_data["id"]).strip()
                remote_id = str(real_product_id).strip()
                
                if config_id == remote_id:
                    if user_db.plan != p_name:
                        print(f"UPDATING PLAN: {user_db.plan} -> {p_name}")
                        user_db.plan = p_name
                        user_db.billing_cycle = cycle
                    found_plan = True
                    break
            if found_plan: break
        
        if not found_plan:
            print(f"WARNING: Product ID {real_product_id} not found in PLAN_CONFIG.")

        # 6. Force Save
        db.add(user_db)
        db.commit()
        db.refresh(user_db) # Reload to confirm

        return {
            "status": "success", 
            "message": "Subscription synced successfully.",
            "synced_status": user_db.subscription_status,
            "synced_plan": user_db.plan
        }

    except Exception as e:
        print(f"SYNC ERROR: {e}")
        logger.error(f"Sync Failed: {e}")
        # Handle remote deletion
        if "404" in str(e):
             user_db.subscription_status = "canceled"
             user_db.subscription_id = None
             db.commit()
             return {"status": "success", "message": "Subscription was deleted remotely."}
        raise HTTPException(status_code=500, detail="Failed to sync subscription.")