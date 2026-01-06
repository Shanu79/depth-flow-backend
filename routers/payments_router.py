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
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # 1. Validate Plan
    cycle_data = PLAN_CONFIG.get(request.billing_cycle)
    if not cycle_data:
        raise HTTPException(status_code=400, detail="Invalid billing cycle")

    plan_data = cycle_data.get(request.plan_name)
    if not plan_data:
        raise HTTPException(status_code=400, detail=f"Invalid plan: {request.plan_name}")

    target_product_id = plan_data["id"]

    # ... Status Check Logic (Same as before) ...
    current_status = current_user.subscription_status or ""
    has_active_sub = current_user.subscription_id and (
        current_status == "active" or "Scheduled for cancellation" in current_status
    )

    try:
        # --- SCENARIO A: UPGRADE / DOWNGRADE / REACTIVATE ---
        if has_active_sub:
            
            # 1. Reactivate if needed
            if "Scheduled for cancellation" in current_status:
                try:
                    client.subscriptions.update(
                        subscription_id=current_user.subscription_id,
                        cancel_at_next_billing_date=False 
                    )
                except Exception:
                    pass 

            # 2. Change Plan (Wrapped in specific Error Handling)
            try:
                updated_sub = client.subscriptions.change_plan(
                    subscription_id=current_user.subscription_id,
                    product_id=target_product_id,
                    proration_billing_mode="prorated_immediately",
                    quantity=request.quantity
                )
            except Exception as e:
                # --- NEW: Handle Pending Payment Lock ---
                error_str = str(e)
                if "PREVIOUS_PAYMENT_PENDING" in error_str or "409" in error_str:
                    logger.warning(f"409 Error for user {current_user.id}: Pending Payment")
                    raise HTTPException(
                        status_code=409, 
                        detail="A previous payment is currently processing. Please wait a few minutes for it to settle before changing plans."
                    )
                else:
                    raise e # Re-raise other errors to be caught by the outer block

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

        # --- SCENARIO B: NEW SUBSCRIPTION (Same as before) ---
        else:
            credits_to_add = plan_data["credits"]
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
            return {"action": "checkout", "checkout_url": session.checkout_url}

    except HTTPException as he:
        raise he # Pass through our custom HTTPExceptions
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
        # This fires for: New Subs, Upgrades, AND Auto-Renewals
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
                # Check if it's a renewal (same Subscription ID)
                is_renewal = user.subscription_id == new_sub_id
                
                # 2. Determine Credits to Add
                credits_to_add = 0
                source = "Unknown"

                if "credits_to_add" in metadata:
                    # Case A: New Subscription (Metadata from Checkout)
                    credits_to_add = int(metadata.get("credits_to_add"))
                    source = "Metadata (New Sub)"
                elif is_renewal:
                    # Case B: Auto-Renewal or Plan Change (No Metadata)
                    # We look up the credits based on the user's CURRENT active plan in DB
                    current_cycle = PLAN_CONFIG.get(user.billing_cycle, {})
                    current_plan = current_cycle.get(user.plan, {})
                    credits_to_add = current_plan.get("credits", 0)
                    source = f"Auto-Renewal ({user.plan})"

                # 3. Add Credits
                if credits_to_add > 0:
                    user.credits += credits_to_add
                    logger.info(f"ADDED {credits_to_add} CREDITS to User {user.email}. Source: {source}")
                else:
                    logger.warning(f"Payment succeeded but 0 credits added. User: {user.email}, Source: {source}")

                # 4. Sync State
                if new_sub_id:
                    user.subscription_id = new_sub_id
                    user.subscription_status = "active"
                
                # Apply metadata updates if present (only on New Subs)
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
                user.subscription_status = "canceled"
                user.plan = "Free"
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
    Force-syncs the local database with Dodo Payments.
    Call this when the user visits the billing page or reports an issue.
    """
    if not current_user.subscription_id:
        return {"status": "ignored", "message": "No active subscription to sync."}

    try:
        # 1. Fetch the REAL status from Dodo
        # We use .retrieve() (or .get() depending on exact SDK version)
        dodo_sub = client.subscriptions.retrieve(
            subscription_id=current_user.subscription_id
        )

        # 2. Map Dodo Data to Local Fields
        real_status = dodo_sub.status  # e.g., 'active', 'on_hold', 'cancelled'
        real_product_id = dodo_sub.product_id
        
        # Check for "Scheduled Cancellation" logic
        # If Dodo says active but 'cancel_at_period_end' is true
        is_scheduled_cancel = getattr(dodo_sub, 'cancel_at_next_billing_date', False)
        
        # 3. Determine the Status String
        if real_status == "active" and is_scheduled_cancel:
            # Reconstruct the "Scheduled for..." string
            next_date = getattr(dodo_sub, 'next_billing_date', None)
            fmt_date = str(next_date)[:10] if next_date else "soon"
            final_status = f"Scheduled for cancellation on {fmt_date}"
        else:
            final_status = real_status

        # 4. Update Database
        current_user.subscription_status = final_status
        
        # Sync Plan (Reverse Lookup ID -> Name)
        # This fixes cases where a user upgraded but DB didn't update
        found_plan = False
        for cycle, plans in PLAN_CONFIG.items():
            for p_name, p_data in plans.items():
                if p_data["id"] == real_product_id:
                    current_user.plan = p_name
                    current_user.billing_cycle = cycle
                    found_plan = True
                    break
            if found_plan: break
            
        db.commit()
        logger.info(f"Synced User {current_user.id}: Status {final_status}, Plan {current_user.plan}")

        return {
            "status": "success", 
            "message": "Subscription synced successfully.",
            "synced_status": final_status,
            "synced_plan": current_user.plan
        }

    except Exception as e:
        logger.error(f"Sync Failed: {e}")
        # If Dodo returns 404, it means the sub is deleted. Handle that:
        if "404" in str(e):
             current_user.subscription_status = "canceled"
             current_user.subscription_id = None
             db.commit()
             return {"status": "success", "message": "Subscription was deleted remotely."}
             
        raise HTTPException(status_code=500, detail="Failed to sync subscription.")