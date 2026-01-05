import os
import logging
import requests 
from fastapi import APIRouter, HTTPException, Depends, Header, Request
from pydantic import BaseModel
from typing import Optional
from dodopayments import DodoPayments
from sqlalchemy.orm import Session
from standardwebhooks.webhooks import Webhook as StandardWebhook

from database import get_db
from models import User
from auth import get_current_user

# --- 1. SETUP LOGGING ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/payments",
    tags=["payments"]
)

# --- 2. CONFIGURATION ---
DODO_API_KEY = os.environ.get("DODO_PAYMENTS_API_KEY")
WEBHOOK_SECRET = os.environ.get("DODO_WEBHOOK_SECRET")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

if not DODO_API_KEY or not WEBHOOK_SECRET:
    logger.warning("CRITICAL: Dodo API Key or Webhook Secret is missing!")

client = DodoPayments(
    bearer_token=DODO_API_KEY,
    environment="live_mode" 
)

DODO_API_URL = "https://api.dodopayments.com/v1" 

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

# --- HELPER: CANCELLATION LOGIC ---
def cancel_dodo_subscription_raw(subscription_id: str):
    """
    Cancels a subscription using the raw Dodo API.
    """
    try:
        url = f"{DODO_API_URL}/subscriptions/{subscription_id}/cancel"
        headers = {
            "Authorization": f"Bearer {DODO_API_KEY}",
            "Content-Type": "application/json"
        }
        
        response = requests.post(url, headers=headers)
        
        if response.status_code == 200:
            return True
        elif response.status_code == 404:
            return True 
        
        # Raise error for unexpected failures
        response.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Raw API Cancel failed: {e}")
        # We don't raise here to allow local DB cleanup to proceed
        pass

# --- 3. ENDPOINTS ---

@router.post("/create-checkout-session")
async def create_checkout_session(
    request: CheckoutRequest, 
    current_user: User = Depends(get_current_user)
):
    try:
        cycle_data = PLAN_CONFIG.get(request.billing_cycle)
        if not cycle_data:
            raise HTTPException(status_code=400, detail="Invalid billing cycle")

        plan_data = cycle_data.get(request.plan_name)
        if not plan_data:
            raise HTTPException(status_code=400, detail=f"Invalid plan: {request.plan_name}")

        product_id = plan_data["id"]
        credits_to_add = plan_data["credits"]

        session = client.checkout_sessions.create(
            product_cart=[{
                "product_id": product_id,
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
        
        return {"checkout_url": session.checkout_url}

    except Exception as e:
        logger.error(f"Payment Init Error: {e}")
        raise HTTPException(status_code=400, detail=f"Payment Gateway Error: {str(e)}")


@router.post("/cancel-subscription")
async def cancel_subscription(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not current_user.subscription_id:
        raise HTTPException(status_code=400, detail="No active subscription found.")

    try:
        # 1. Call Dodo API
        cancel_dodo_subscription_raw(current_user.subscription_id)

        # 2. Update Local Database
        current_user.subscription_status = "canceled"
        current_user.subscription_id = None 
        current_user.billing_cycle = None 
        
        db.commit()

        return {"status": "success", "message": "Subscription cancelled successfully."}

    except Exception as e:
        logger.error(f"Cancellation Failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to cancel: {str(e)}")


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
        except Exception as e:
            logger.error(f"Webhook Signature Verification Failed: {e}")
            raise HTTPException(status_code=400, detail="Invalid signature")

        # 2. Parse Event
        payload = await request.json()
        event_type = payload.get("type")
        data = payload.get("data", {})
        
        logger.info(f"Webhook Event: {event_type}")

        # --- EVENT: PAYMENT SUCCEEDED ---
        if event_type == "payment.succeeded":
            metadata = data.get("metadata", {})
            user_id = metadata.get("user_id")
            plan_name = metadata.get("plan_name")
            billing_cycle = metadata.get("billing_cycle")
            new_subscription_id = data.get("subscription_id")
            credits = int(metadata.get("credits_to_add", 0))

            user = None
            if user_id:
                user = db.query(User).filter(User.id == int(user_id)).first()
            if not user and new_subscription_id:
                user = db.query(User).filter(User.subscription_id == new_subscription_id).first()
            if not user: 
                 email = data.get("customer", {}).get("email")
                 if email: user = db.query(User).filter(User.email == email).first()

            if user:
                # Upgrade Logic: Cancel old sub if ID changed
                if user.subscription_id and new_subscription_id and user.subscription_id != new_subscription_id:
                    cancel_dodo_subscription_raw(user.subscription_id)

                if credits > 0:
                    user.credits += credits
                
                if new_subscription_id:
                    user.subscription_id = new_subscription_id
                    user.subscription_status = "active"
                
                if plan_name: user.plan = plan_name
                if billing_cycle: user.billing_cycle = billing_cycle

                db.commit()

        # --- EVENT: SUBSCRIPTION CANCELLED ---
        # Handles external cancellations or expirations
        elif event_type == "subscription.cancelled":
            subscription_id = data.get("subscription_id")
            
            if subscription_id:
                user = db.query(User).filter(User.subscription_id == subscription_id).first()
                
                if user:
                    user.subscription_status = "canceled"
                    user.plan = "Free"
                    user.subscription_id = None
                    user.billing_cycle = None
                    db.commit()
                    logger.info(f"Subscription {subscription_id} cancelled via Webhook for {user.email}")
                else:
                    logger.warning(f"Received cancel webhook for unknown subscription: {subscription_id}")

        return {"status": "received"}

    except Exception as e:
        logger.error(f"Webhook Error: {e}")
        return {"status": "error", "detail": str(e)}