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
        "Basic": {"id": "pdt_0NUQxXnGKlkhrpGBAFMvy", "credits": 850},
        "Pro":   {"id": "pdt_0NUQxXJa0TR6vJJznsrt2", "credits": 2000},
        "Premium":   {"id": "pdt_0Nc5Ts7erauRnehAFJV6Q", "credits": 5000},
    },
    "yearly": {
        "Basic": {"id": "pdt_0NUQxWNFaRWTd8eoNkqoJ", "credits": 10200},
        "Pro":   {"id": "pdt_0NUQxVyW088PIAwIzObuC", "credits": 24000},
        "Premium": {"id": "pdt_0Nc5TfN5t1oLCfoeNN7AZ", "credits": 60000},
    }
}

# --- NEW: API PLANS ---
API_PLAN_CONFIG = {
    "monthly": {
        "Starter API": {"id": "pdt_0Nbt4jmPtgBIPmJgF3qdh", "credits": 10000},
        "Growth API":  {"id": "pdt_0NbtLLrNFAwrGkvpH0lGQ", "credits": 30000},
        "Pro API":     {"id": "pdt_0Nbt4bIY0qlHH3u8SMyyD", "credits": 65000},
    },
    "yearly": {
        "Starter API": {"id": "dummy_api_starter_yearly", "credits": 120000},
        "Growth API":  {"id": "dummy_api_growth_yearly", "credits": 324000},
        "Pro API":     {"id": "dummy_api_pro_yearly", "credits": 720000},
    }
}

ONE_TIME_PLANS = {
    # "Trial": {"id": "pdt_0NUQxWtv1A7PDo75MPx9L", "credits": 120},
    "Credit Pack": {"id": "pdt_0NYCtBgPqWWzyF6yKrE98", "credits": 750},
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
    is_api_plan = False

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
            has_workspace = current_user.subscription_id and current_user.subscription_status == "active"
            has_api = current_user.api_subscription_id and current_user.api_subscription_status == "active"
            if not has_workspace and not has_api:
                raise HTTPException(
                    status_code=403, 
                    detail="You must have an active subscription to purchase add-on credit packs."
                )

        credits_to_add = plan_data["credits"]
        is_one_time = True

    # CASE B: RECURRING SUBSCRIPTION
    else:
        # Minimal Check: Is it Workspace or API?
        if request.billing_cycle in PLAN_CONFIG and request.plan_name in PLAN_CONFIG[request.billing_cycle]:
            plan_data = PLAN_CONFIG[request.billing_cycle][request.plan_name]
        elif request.billing_cycle in API_PLAN_CONFIG and request.plan_name in API_PLAN_CONFIG[request.billing_cycle]:
            plan_data = API_PLAN_CONFIG[request.billing_cycle][request.plan_name]
            is_api_plan = True
        else:
            raise HTTPException(status_code=400, detail=f"Invalid plan: {request.plan_name}")

        target_product_id = plan_data["id"]
        credits_to_add = plan_data["credits"]

    # Define active status safely based on plan type
    if is_api_plan:
        current_status = current_user.api_subscription_status or ""
        sub_id = current_user.api_subscription_id
    else:
        current_status = current_user.subscription_status or ""
        sub_id = current_user.subscription_id
        
    has_active_sub = sub_id and (current_status == "active" or "Scheduled for cancellation" in current_status)

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
                        subscription_id=sub_id,
                        cancel_at_next_billing_date=False 
                    )
                except Exception:
                    pass 

            # 2. Change Plan
            try:
                updated_sub = client.subscriptions.change_plan(
                    subscription_id=sub_id,
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
            if is_api_plan:
                current_user.api_plan = request.plan_name
                current_user.api_billing_cycle = request.billing_cycle
                current_user.api_subscription_status = "active"
            else:
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
                "is_one_time": "true" if is_one_time else "false",
                "is_api": "true" if is_api_plan else "false" # <--- Added flag
            }

            logger.info(f"🛒 GENERATING CHECKOUT FOR: {target_product_id}")
            
            return_url = f"{FRONTEND_URL}/api-pricing" if is_api_plan else f"{FRONTEND_URL}/workspace"
            
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
                return_url=return_url, 
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
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    body = await request.json() if request.headers.get("content-length") != "0" else {}
    plan_type = body.get("plan_type", "workspace")

    sub_id = current_user.api_subscription_id if plan_type == "api" else current_user.subscription_id

    if not sub_id:
        raise HTTPException(status_code=400, detail="No active subscription found.")

    try:
        updated_sub = client.subscriptions.update(
            subscription_id=sub_id,
            cancel_at_next_billing_date=True
        )
        
        raw_date = getattr(updated_sub, 'next_billing_date', None)
        formatted_date = str(raw_date)[:10] if raw_date else datetime.now().strftime('%Y-%m-%d')

        new_status = f"Scheduled for cancellation on {formatted_date}"
        
        if plan_type == "api":
            current_user.api_subscription_status = new_status
        else:
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
                is_one_time = metadata.get("is_one_time") == "true"
                is_api = metadata.get("is_api") == "true"
                
                is_renewal = (user.api_subscription_id == new_sub_id) if is_api else (user.subscription_id == new_sub_id)

                # --- 2. DETERMINE CREDITS ---
                credits_to_add = 0
                source = "Unknown"

                if "credits_to_add" in metadata:
                    credits_to_add = int(metadata.get("credits_to_add"))
                    source = "Metadata (New/One-Time)"
                elif is_renewal:
                    config_map = API_PLAN_CONFIG if is_api else PLAN_CONFIG
                    active_cycle = user.api_billing_cycle if is_api else user.billing_cycle
                    active_plan = user.api_plan if is_api else user.plan
                    
                    current_cycle = config_map.get(active_cycle, {})
                    current_plan = current_cycle.get(active_plan, {})
                    credits_to_add = current_plan.get("credits", 0)
                    source = f"Auto-Renewal ({active_plan})"

                # --- 3. ADD CREDITS ---
                if credits_to_add > 0:
                    user.credits += credits_to_add 
                    logger.info(f"ADDED {credits_to_add} CREDITS to User {user.email}. Source: {source}")

                # --- 4. SYNC STATE (PROTECTED) ---
                if is_one_time:
                    if not user.subscription_id or user.subscription_status != "active":
                         user.plan = metadata.get("plan_name", user.plan)
                else:
                    if is_api:
                        if new_sub_id:
                            user.api_subscription_id = new_sub_id
                            user.api_subscription_status = "active"
                        if metadata.get("plan_name"): user.api_plan = metadata["plan_name"]
                        if metadata.get("billing_cycle"): user.api_billing_cycle = metadata["billing_cycle"]
                    else:
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

            user = db.query(User).filter((User.subscription_id == sub_id) | (User.api_subscription_id == sub_id)).first()
            
            if user:
                is_api = user.api_subscription_id == sub_id
                if is_api:
                    user.api_subscription_status = status
                    config_map = API_PLAN_CONFIG
                else:
                    user.subscription_status = status
                    config_map = PLAN_CONFIG
                
                # Keep Plan in Sync
                found_plan = False
                for cycle, plans in config_map.items():
                    for p_name, p_data in plans.items():
                        if p_data["id"] == product_id:
                            if is_api:
                                user.api_plan = p_name
                                user.api_billing_cycle = cycle
                            else:
                                user.plan = p_name
                                user.billing_cycle = cycle
                            found_plan = True
                            break
                    if found_plan: break
                
                db.commit()

        # --- EVENT: SUBSCRIPTION CANCELLED ---
        elif event_type == "subscription.cancelled":
            sub_id = data.get("subscription_id")
            user = db.query(User).filter((User.subscription_id == sub_id) | (User.api_subscription_id == sub_id)).first()
            
            if user:
                logger.info(f"Subscription expired for User {user.email}. Zeroing credits.")
                
                if user.api_subscription_id == sub_id:
                    user.api_subscription_status = "cancelled"
                    user.api_plan = "Free"
                    user.api_subscription_id = None
                    user.api_billing_cycle = None
                elif user.subscription_id == sub_id:
                    user.subscription_status = "cancelled"
                    user.plan = "Free"
                    user.subscription_id = None
                    user.billing_cycle = None
                
                # ZERO OUT CREDITS
                user.credits = 0 
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
    Force-syncs local DB with Dodo for workspace subscriptions.
    """
    if not current_user.subscription_id:
        return {"status": "ignored", "message": "No active subscription to sync."}

    user_db = db.query(User).filter(User.id == current_user.id).first()
    
    if not user_db:
        raise HTTPException(status_code=404, detail="User not found in DB")

    try:
        dodo_sub = client.subscriptions.retrieve(
            subscription_id=user_db.subscription_id
        )

        real_status = dodo_sub.status 
        real_product_id = dodo_sub.product_id
        
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

        if user_db.subscription_status != final_status:
             user_db.subscription_status = final_status

        found_plan = False
        for cycle, plans in PLAN_CONFIG.items():
            for p_name, p_data in plans.items():
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
        
        db.add(user_db)
        db.commit()
        db.refresh(user_db) 

        return {
            "status": "success", 
            "message": "Subscription synced successfully.",
            "synced_status": user_db.subscription_status,
            "synced_plan": user_db.plan
        }

    except Exception as e:
        print(f"SYNC ERROR: {e}")
        logger.error(f"Sync Failed: {e}")
        if "404" in str(e):
             user_db.subscription_status = "canceled"
             user_db.subscription_id = None
             db.commit()
             return {"status": "success", "message": "Subscription was deleted remotely."}
        raise HTTPException(status_code=500, detail="Failed to sync subscription.")

# --- INVOICE HISTORY ---
@router.get("/history")
async def get_payment_history(
    current_user: User = Depends(get_current_user)
):
    try:
        customers = client.customers.list(email=current_user.email, limit=1)
        if not customers.items:
            return [] 
        
        customer_id = customers.items[0].customer_id
        payments = client.payments.list(customer_id=customer_id, limit=20)
        
        history = []
        for payment in payments.items:
            payment_id = getattr(payment, 'payment_id', getattr(payment, 'id', 'Unknown'))
            amount = getattr(payment, 'total_amount', 0)
            status = getattr(payment, 'status', 'pending')
            created_at = getattr(payment, 'created_at', datetime.now())
            invoice_url = getattr(payment, 'receipt_url', getattr(payment, 'invoice_url', None))

            history.append({
                "id": payment_id,
                "amount": amount, 
                "status": status,
                "created_at": str(created_at),
                "invoice_url": invoice_url
            })
            
        return history
    except Exception as e:
        logger.error(f"Failed to fetch payment history: {e}")
        return []