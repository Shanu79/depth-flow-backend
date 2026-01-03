import os
import logging
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
# In production, this pushes logs to your server's output stream
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/payments",
    tags=["payments"]
)

# --- 2. CONFIGURATION ---
# Load secrets securely from environment
DODO_API_KEY = os.environ.get("DODO_PAYMENTS_API_KEY")  # Use test key for development
WEBHOOK_SECRET = os.environ.get("DODO_WEBHOOK_SECRET")

#frontend URL
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

if not DODO_API_KEY or not WEBHOOK_SECRET:
    logger.warning("CRITICAL: Dodo API Key or Webhook Secret is missing!")

client = DodoPayments(
    bearer_token=DODO_API_KEY,
    environment="live_mode" # Change to "live_mode" when ready
)

# Centralized configuration for Plans & Credits
# Update these IDs with your actual Dashboard IDs
PLAN_CONFIG = {
    "monthly": {
        "Basic": {"id": "pdt_0NUQxXnGKlkhrpGBAFMvy", "credits": 550},
        "Pro":   {"id": "pdt_0NUQxXJa0TR6vJJznsrt2", "credits": 1200},
        "Free":  {"id": "pdt_0NUQxWtv1A7PDo75MPx9L", "credits": 0},
    },
    "yearly": {
        "Basic": {"id": "pdt_0NUQxWNFaRWTd8eoNkqoJ", "credits": 6600}, # Example: 12 * 600
        "Pro":   {"id": "pdt_0NUQxVyW088PIAwIzObuC", "credits": 14400},
        "Free":  {"id": "pdt_0NUQxWtv1A7PDo75MPx9L", "credits": 0},
    }
}

class CheckoutRequest(BaseModel):
    plan_name: str
    billing_cycle: str
    quantity: int = 1

# --- 3. ENDPOINTS ---

@router.post("/create-checkout-session")
async def create_checkout_session(
    request: CheckoutRequest, 
    current_user: User = Depends(get_current_user)
):
    try:
        # Validate Billing Cycle
        cycle_data = PLAN_CONFIG.get(request.billing_cycle)
        if not cycle_data:
            raise HTTPException(status_code=400, detail="Invalid billing cycle")

        # Validate Plan
        plan_data = cycle_data.get(request.plan_name)
        if not plan_data:
            raise HTTPException(status_code=400, detail=f"Invalid plan: {request.plan_name}")

        product_id = plan_data["id"]
        credits_to_add = plan_data["credits"]

        # Create Session
        session = client.checkout_sessions.create(
            product_cart=[{
                "product_id": product_id,
                "quantity": request.quantity
            }],
            customer={
                "email": current_user.email,
                "name": current_user.full_name or "User"
            },
            # Metadata: This is the "state" we pass to the webhook
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
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/webhook")
async def dodo_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Secure Webhook Handler.
    Verifies signature and updates user credits (Handles Initial + Renewals).
    """
    try:
        # 1. Get Raw Body & Headers for Verification
        payload_bytes = await request.body()
        payload_str = payload_bytes.decode("utf-8")
        headers = dict(request.headers)

        # 2. Verify Signature (SECURITY CRITICAL)
        try:
            wh = StandardWebhook(WEBHOOK_SECRET)
            wh.verify(payload_str, headers)
        except Exception as e:
            logger.error(f"Webhook Signature Verification Failed: {e}")
            raise HTTPException(status_code=400, detail="Invalid signature")

        # 3. Process Event
        payload = await request.json()
        event_type = payload.get("type")
        data = payload.get("data", {})
        
        # Log event for debugging
        payment_id = data.get("payment_id")
        logger.info(f"Webhook Event: {event_type} | Payment ID: {payment_id}")

        # ---------------------------------------------------------
        # 1. HANDLE SUCCESSFUL PAYMENTS (Checkout OR Renewal)
        # ---------------------------------------------------------
        if event_type == "payment.succeeded":
            metadata = data.get("metadata", {})
            user_id = metadata.get("user_id")
            
            # Strategy A: Try getting credits from metadata (Works for First Checkout)
            credits = int(metadata.get("credits_to_add", 0))

            # Strategy B: RENEWAL LOGIC (If metadata is missing)
            if credits == 0:
                logger.info("Metadata missing. Attempting to identify plan from Product ID (Renewal context).")
                
                # Get the product ID from the webhook payload
                purchased_product_id = data.get("product_id") 

                # Look up credits in your PLAN_CONFIG
                found_plan = False
                for cycle, plans in PLAN_CONFIG.items():
                    for name, details in plans.items():
                        if details["id"] == purchased_product_id:
                            credits = details["credits"]
                            found_plan = True
                            break
                    if found_plan: break
                
                if not found_plan:
                    logger.error(f"CRITICAL: Unknown Product ID {purchased_product_id} in renewal.")
                    return {"status": "error", "reason": "unknown_product"}

            # --- USER LOOKUP (Crucial for Renewals) ---
            user = None
            
            # 1. Try ID (from Metadata - only exists on first checkout)
            if user_id:
                user = db.query(User).filter(User.id == int(user_id)).first()
            
            # 2. Try Subscription ID (This is how we find users on Renewals!)
            subscription_id = data.get("subscription_id")
            if not user and subscription_id:
                user = db.query(User).filter(User.subscription_id == subscription_id).first()
                
            # 3. Try Email (Last Resort fallback)
            if not user:
                customer_email = data.get("customer", {}).get("email")
                if customer_email:
                    user = db.query(User).filter(User.email == customer_email).first()

            # --- UPDATE DATABASE ---
            if user and credits > 0:
                # Add Credits
                user.credits += credits
                
                # Update subscription details if available
                if subscription_id:
                    user.subscription_id = subscription_id
                    user.subscription_status = "active"
                
                # Update plan/cycle only if metadata exists (usually first checkout)
                plan_name = metadata.get("plan_name")
                billing_cycle = metadata.get("billing_cycle")
                if plan_name: user.plan = plan_name
                if billing_cycle: user.billing_cycle = billing_cycle

                db.commit()
                logger.info(f"SUCCESS: User {user.email} credited +{credits}")
            else:
                logger.error(f"FAILED: Could not find user for payment {payment_id} or credits were 0")

        # ---------------------------------------------------------
        # 2. HANDLE FAILURES
        # ---------------------------------------------------------
        elif event_type == "payment.failed":
            metadata = data.get("metadata", {})
            user_id = metadata.get("user_id")
            error_reason = data.get("error_message", "Unknown error")
            logger.warning(f"PAYMENT FAILED: User {user_id} failed. Reason: {error_reason}")

        # ---------------------------------------------------------
        # 3. HANDLE REFUNDS
        # ---------------------------------------------------------
        elif event_type == "refund.succeeded":
            metadata = data.get("metadata", {})
            user_id = metadata.get("user_id")
            credits_to_remove = int(metadata.get("credits_to_add", 0))

            if user_id and credits_to_remove > 0:
                user = db.query(User).filter(User.id == int(user_id)).first()
                if user:
                    user.credits = max(0, user.credits - credits_to_remove)
                    db.commit()
                    logger.info(f"REFUND: Removed {credits_to_remove} credits from {user.email}")

        return {"status": "received"}

    except Exception as e:
        logger.error(f"Webhook Error: {e}")
        return {"status": "error", "detail": str(e)}