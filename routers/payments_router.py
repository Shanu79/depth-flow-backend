import os
import logging
import base64
import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Request, BackgroundTasks
from pydantic import BaseModel
from google.oauth2 import service_account
from googleapiclient.discovery import build
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
        "Basic": {"id": "pdt_0NUQxXnGKlkhrpGBAFMvy", "credits": 600},
        "Pro":   {"id": "pdt_0NUQxXJa0TR6vJJznsrt2", "credits": 1500},
        "Premium":   {"id": "pdt_0Nc5Ts7erauRnehAFJV6Q", "credits": 4000},
    },
    "yearly": {
        "Basic": {"id": "pdt_0NUQxWNFaRWTd8eoNkqoJ", "credits": 7200},
        "Pro":   {"id": "pdt_0NUQxVyW088PIAwIzObuC", "credits": 18000},
        "Premium": {"id": "pdt_0Nc5TfN5t1oLCfoeNN7AZ", "credits": 48000},
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
    "Credit Pack": {"id": "pdt_0NYCtBgPqWWzyF6yKrE98", "credits": 900},
}

# Add this alongside your other PLAN_CONFIGs
GOOGLE_PLAY_PLANS = {
    "sub_basic_mo": 600,
    "sub_pro_mo": 1500,
    "sub_basic_yr": 7200,
    "sub_pro_yr": 18000
}

async def process_renewal(purchase_token: str, db: Session):
    try:
        # 1. Find the user by the token (REQUIRES FIX #2 BELOW)
        user = db.query(User).filter(User.play_purchase_token == purchase_token).first()
        if not user:
            logger.error(f"Renewal failed: No user found for token {purchase_token}")
            return

        # 2. Authenticate with Google Play API
        credentials = service_account.Credentials.from_service_account_file('google-play-service-account.json')
        service = build('androidpublisher', 'v3', credentials=credentials)

        # We need to know the product_id (e.g., sub_pro_mo) to check the subscription
        # Assuming you store this on the user, or infer it from user.plan
        # If you don't store product_id, you can map it backwards from user.plan & user.billing_cycle
        product_id = f"sub_{user.plan.lower()}_{'yr' if user.billing_cycle == 'yearly' else 'mo'}"

        # 3. Call Google API to get the latest state
        result = service.purchases().subscriptions().get( 
            packageName="com.shin.depthflow",
            subscriptionId=product_id,
            token=purchase_token
        ).execute()

        # paymentState 1 = Payment received. 
        if result.get('paymentState') == 1:
            order_id = result.get('orderId')
            
            # Idempotency: Ensure we haven't already processed this exact renewal order
            if user.last_payment_id == order_id:
                logger.info(f"Renewal already processed for order: {order_id}")
                return

            # 4. Apply Credits
            total_credits = GOOGLE_PLAY_PLANS.get(product_id, 0)
            if user.billing_cycle == "yearly":
                user.credits = total_credits // 12
                user.next_credit_drop_date = datetime.utcnow() + timedelta(days=30)
            else:
                user.credits = total_credits

            user.last_payment_id = order_id
            db.commit()
            logger.info(f"Successfully processed background renewal for {user.email}")
        else:
            logger.warning(f"Subscription renewal not in valid state: {result.get('paymentState')}")

    except Exception as e:
        logger.error(f"Failed to process background renewal: {e}")

class CheckoutRequest(BaseModel):
    plan_name: str
    billing_cycle: str
    quantity: int = 1

# Safer version if the dictionary approach fails
def check_if_already_purchased(email: str, product_id: str) -> bool:
    try:
        # 1. Get Customer ID first
        customers = client.customers.list(email=email)
        if not customers.items:
            return False # Customer doesn't exist, so they haven't bought anything
        
        customer_id = customers.items[0].customer_id

        # 2. List payments using the ID string
        payments = client.payments.list(customer_id=customer_id)

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

        if request.plan_name == "Trial":
            if current_user.plan == "Trial":
                raise HTTPException(status_code=403, detail="You already have the Trial pack.")
            
            already_bought = check_if_already_purchased(current_user.email, target_product_id)
            if already_bought:
                raise HTTPException(status_code=403, detail="You have already used the Trial pack in the past.")

        if request.plan_name == "Credit Pack":
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
        if request.billing_cycle in PLAN_CONFIG and request.plan_name in PLAN_CONFIG[request.billing_cycle]:
            plan_data = PLAN_CONFIG[request.billing_cycle][request.plan_name]
        elif request.billing_cycle in API_PLAN_CONFIG and request.plan_name in API_PLAN_CONFIG[request.billing_cycle]:
            plan_data = API_PLAN_CONFIG[request.billing_cycle][request.plan_name]
            is_api_plan = True
        else:
            raise HTTPException(status_code=400, detail=f"Invalid plan: {request.plan_name}")

        target_product_id = plan_data["id"]
        credits_to_add = plan_data["credits"]

    # Grab the old sub ID so we can cancel it AFTER they pay for the new one
    if is_api_plan:
        current_status = current_user.api_subscription_status or ""
        sub_id = current_user.api_subscription_id
    else:
        current_status = current_user.subscription_status or ""
        sub_id = current_user.subscription_id
        
    has_active_sub = sub_id and (current_status == "active" or "Scheduled for cancellation" in current_status)
    old_sub_id = sub_id if has_active_sub and not is_one_time else ""

    # ---------------------------------------------------------
    # STEP 2: EXECUTE PAYMENT (Always Checkout to reset cycle)
    # ---------------------------------------------------------
    
    try:
        # We pass old_sub_id in metadata. If the user closes the checkout tab, 
        # nothing happens and their current plan remains safely active.
        metadata = {
            "user_id": str(current_user.id),
            "credits_to_add": str(credits_to_add),
            "plan_name": request.plan_name,
            "billing_cycle": request.billing_cycle,
            "is_one_time": "true" if is_one_time else "false",
            "is_api": "true" if is_api_plan else "false",
            "old_sub_id": old_sub_id 
        }

        # --- FIX 2: PREVENT DUPLICATE CUSTOMER PROFILES ---
        customer_payload = {
            "email": current_user.email,
            "name": current_user.full_name or "User"
        }
        try:
            # Query Dodo to see if this email already belongs to a customer
            existing_customers = client.customers.list(email=current_user.email)
            if existing_customers.items:
                # Use the existing customer ID instead of sending the dictionary
                customer_payload = {"customer_id": existing_customers.items[0].customer_id}
        except Exception as e:
            logger.warning(f"Could not search for existing Dodo customer: {e}")

        logger.info(f"🛒 GENERATING CHECKOUT FOR: {target_product_id}")
        
        return_url = f"{FRONTEND_URL}/api-pricing" if is_api_plan else f"{FRONTEND_URL}/workspace"
        
        session = client.checkout_sessions.create(
            product_cart=[{
                "product_id": target_product_id, 
                "quantity": request.quantity
            }],
            customer=customer_payload,  # <-- Injected safe payload here
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
            payment_id = data.get("payment_id", data.get("id")) # Get unique payment ID
            
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

                # --- FIX 3: IDEMPOTENCY CHECK (PREVENT DOUBLE CREDITS) ---
                if payment_id and hasattr(user, "last_payment_id"):
                    if user.last_payment_id == payment_id and not is_renewal:
                        logger.info(f"Duplicate webhook ignored for payment: {payment_id}")
                        return {"status": "ignored", "message": "Duplicate payment webhook"}

                # --- 2. DETERMINE CREDITS ---
                credits_to_add = 0
                source = "Unknown"

                if "credits_to_add" in metadata:
                    credits_to_add = int(metadata.get("credits_to_add"))
                    source = "Metadata (New/One-Time/Upgrade)"
                elif is_renewal:
                    config_map = API_PLAN_CONFIG if is_api else PLAN_CONFIG
                    active_cycle = user.api_billing_cycle if is_api else user.billing_cycle
                    active_plan = user.api_plan if is_api else user.plan
                    
                    current_cycle = config_map.get(active_cycle, {})
                    current_plan = current_cycle.get(active_plan, {})
                    credits_to_add = current_plan.get("credits", 0)
                    source = f"Auto-Renewal ({active_plan})"

                # --- 3. ADD / RESET CREDITS ---
                if credits_to_add > 0:
                    
                    # 1. Determine if this is a yearly subscription payment
                    active_cycle = metadata.get("billing_cycle") or (user.api_billing_cycle if is_api else user.billing_cycle)
                    is_yearly_sub = (active_cycle == "yearly") and not is_one_time
                    
                    actual_credits = credits_to_add
                    
                    # 2. Modify drop amounts and set schedule if yearly
                    if is_yearly_sub:
                        actual_credits = credits_to_add // 12
                        
                        # Set the next drop date to 30 days from now (requires timedelta import at the top)
                        user.next_credit_drop_date = datetime.utcnow() + timedelta(days=30)
                        source += " (1/12th Monthly Drop)"
                    elif not is_one_time:
                        # Clear the drop date if they switched to a monthly plan
                        user.next_credit_drop_date = None

                    # 3. Apply credits
                    if is_renewal:
                        # USE IT OR LOSE IT: Reset the balance
                        user.credits = actual_credits 
                        logger.info(f"RENEWAL: Reset User {user.email} balance to {actual_credits}. Old credits expired.")
                    else:
                        # UPGRADES / CREDIT PACKS / NEW SUBS: Add on top
                        user.credits += actual_credits 
                        logger.info(f"ADDED {actual_credits} CREDITS to User {user.email}. Source: {source}")

                # --- 4. SYNC STATE (SAVE NEW SUB ID) ---
                if is_one_time:
                    
                    if metadata.get("plan_name") == "Trial":
                        if not user.subscription_id or user.subscription_status != "active":
                             user.plan = "Trial"
                    
                    # NEW: Upgrade "Free" users to "Credit Pack" so they bypass the watermark.
                    # Pro and Basic users will be ignored by this block and keep their existing plans!
                    elif metadata.get("plan_name") == "Credit Pack":
                        if user.plan == "Free":
                            user.plan = "Credit Pack"

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

                # Log the successful payment ID for idempotency
                if payment_id and hasattr(user, "last_payment_id"):
                    user.last_payment_id = payment_id

                db.commit() # Important: Commit the NEW sub_id first

                # --- 5. CLEANUP OLD SUBSCRIPTION ---
                old_sub_id = metadata.get("old_sub_id")
                if old_sub_id and old_sub_id != new_sub_id:
                    try:
                        # FIX: Use the SDK's supported update method to schedule the cancellation
                        client.subscriptions.update(
                            subscription_id=old_sub_id,
                            cancel_at_next_billing_date=True
                        )
                        logger.info(f"Cancelled old subscription {old_sub_id} to prevent double billing.")
                    except Exception as e:
                        logger.error(f"Failed to cancel old sub {old_sub_id}: {e}")

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
                user.next_credit_drop_date = None # <-- MAKE SURE THIS LINE IS HERE
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
             user_db.subscription_status = "cancelled"
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
        customers = client.customers.list(email=current_user.email)
        if not customers.items:
            return [] 
        
        customer_id = customers.items[0].customer_id
        payments = client.payments.list(customer_id=customer_id)
        
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
    
class GooglePlayPurchaseReq(BaseModel):
    product_id: str
    purchase_token: str

@router.post("/verify-purchase")
async def verify_google_play_purchase(
    request: GooglePlayPurchaseReq,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    try:
        # 1. Reconstruct credentials from individual environment variables
        # We check for GOOGLE_PRIVATE_KEY as our indicator that manual config is used
        if os.environ.get("GOOGLE_PRIVATE_KEY"):
            creds_dict = {
                "type": "service_account",
                "project_id": os.environ.get("GOOGLE_PROJECT_ID"),
                "private_key_id": os.environ.get("GOOGLE_PRIVATE_KEY_ID"),
                # This line is the most important part of your fix!
                "private_key": os.environ.get("GOOGLE_PRIVATE_KEY").replace("\\n", "\n").replace('"', ''),
                "client_email": os.environ.get("GOOGLE_CLIENT_EMAIL"),
                "client_id": os.environ.get("GOOGLE_CLIENT_ID"),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                "client_x509_cert_url": os.environ.get("GOOGLE_CLIENT_CERT_URL")
            }
            credentials = service_account.Credentials.from_service_account_info(creds_dict)
        else:
            # Fallback for local testing (file-based)
            credentials = service_account.Credentials.from_service_account_file('google-play-service-account.json')
            
        service = build('androidpublisher', 'v3', credentials=credentials)
        
        product_id = request.product_id
        is_subscription = product_id.startswith("sub_")

        # -------------------------------------------------------------
        # 2. CALL THE CORRECT GOOGLE API ENDPOINT
        # -------------------------------------------------------------
        if is_subscription:
            result = service.purchases().subscriptions().get(  # pylint: disable=no-member
                packageName="com.shin.depthflow",
                subscriptionId=product_id,
                token=request.purchase_token
            ).execute()
            
            # Subscriptions use 'paymentState' (1 = Payment received, 2 = Free Trial)
            is_valid_purchase = result.get('paymentState') in [1, 2] 
            
        else:
            result = service.purchases().products().get(  # pylint: disable=no-member
                packageName="com.shin.depthflow",
                productId=product_id,
                token=request.purchase_token
            ).execute()
            
            # One-time products use 'purchaseState' (0 = Purchased)
            is_valid_purchase = result.get('purchaseState') == 0

        # -------------------------------------------------------------
        # 3. PROCESS THE VERIFIED PURCHASE
        # -------------------------------------------------------------
        if is_valid_purchase:
            
            # Use orderId for idempotency, NOT the static purchase token
            order_id = result.get('orderId', request.purchase_token)
            
            if hasattr(current_user, "last_payment_id") and current_user.last_payment_id == order_id:
                return {"status": "ignored", "message": "Purchase already verified", "new_balance": current_user.credits}

            if product_id not in GOOGLE_PLAY_PLANS:
                raise HTTPException(status_code=400, detail="Unknown Google Play Product ID")

            total_credits = GOOGLE_PLAY_PLANS[product_id]
            
            # --- APPLY CREDITS & SPLIT YEARLY LOGIC ---
            if not is_subscription:
                current_user.credits += total_credits
            else:
                is_yearly = product_id.endswith("_yr")
                
                # FIX: Make new subscription purchases additive so we don't wipe out Credit Packs!
                if is_yearly:
                    current_user.credits += (total_credits // 12)
                    current_user.next_credit_drop_date = datetime.utcnow() + timedelta(days=30)
                    current_user.billing_cycle = "yearly"
                else:
                    current_user.credits += total_credits
                    current_user.next_credit_drop_date = None
                    current_user.billing_cycle = "monthly"

                if "basic" in product_id:
                    current_user.plan = "Basic"
                elif "pro" in product_id:
                    current_user.plan = "Pro"
                
                current_user.subscription_status = "active"
                
                # IMPORTANT: Save the token so the background webhook can find this user later
                current_user.play_purchase_token = request.purchase_token

            # Save the unique order_id to prevent double-crediting
            current_user.last_payment_id = order_id
            db.commit()
            
            return {
                "status": "success", 
                "plan": current_user.plan,
                "billing_cycle": current_user.billing_cycle,
                "new_balance": current_user.credits
            }
        else:
            raise HTTPException(status_code=400, detail="Purchase not completed or was cancelled")
            
    except Exception as e:
        logger.error(f"Google Play Verification Failed: {str(e)}")
        raise HTTPException(status_code=400, detail="Invalid purchase token or API error")
    
# --- GOOGLE PLAY RTDN WEBHOOK ---
@router.post("/google-play-webhook")
async def google_play_rtdn(
    request: Request, 
    background_tasks: BackgroundTasks, 
    db: Session = Depends(get_db)
):
    try:
        # 1. Get the Pub/Sub message
        body = await request.json()
        encoded_data = body.get("message", {}).get("data", "")
        
        if not encoded_data:
            return {"status": "ignored", "reason": "No data"}

        # 2. Decode the Base64 payload
        decoded_data = base64.b64decode(encoded_data).decode('utf-8')
        notification = json.loads(decoded_data)
        
        sub_notification = notification.get("subscriptionNotification")
        if not sub_notification:
            return {"status": "ignored", "reason": "Not a subscription event"}
            
        notification_type = sub_notification.get("notificationType")
        purchase_token = sub_notification.get("purchaseToken")
        
        # Notification Type 2 = SUBSCRIPTION_RENEWED
        # You might also want to handle Type 3 (CANCELED) to remove their plan
        if notification_type == 2:
            logger.info("Received Google Play renewal notification. Queueing task.")
            # Offload to background so we reply to Google with 200 OK instantly
            background_tasks.add_task(process_renewal, purchase_token, db)
            
        return {"status": "success"} 

    except Exception as e:
        logger.error(f"Google Play Webhook Error: {e}")
        # Return 200 even on error, otherwise Google Pub/Sub will keep retrying forever
        return {"status": "error", "detail": str(e)}