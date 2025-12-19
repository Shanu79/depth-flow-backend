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

if not DODO_API_KEY or not WEBHOOK_SECRET:
    logger.warning("CRITICAL: Dodo API Key or Webhook Secret is missing!")

client = DodoPayments(
    bearer_token=DODO_API_KEY,
    environment="test_mode" # Change to "live_mode" when ready
)

# Centralized configuration for Plans & Credits
# Update these IDs with your actual Dashboard IDs
PLAN_CONFIG = {
    "monthly": {
        "Basic": {"id": "pdt_0NUPe15y0JO1bh0nqtE9e", "credits": 600},
        "Pro":   {"id": "pdt_0NUPeVOcEvNPCNYVV3LgA", "credits": 1200},
        "Free":  {"id": "pdt_0NUPev23NKkMzid3Mivbq", "credits": 0},
    },
    "yearly": {
        "Basic": {"id": "pdt_0NUPhhIR8EDmFZhXlEXU4", "credits": 7200}, # Example: 12 * 600
        "Pro":   {"id": "pdt_0NUPhrqLTrV4gHrXi5CoD", "credits": 14400},
        "Free":  {"id": "pdt_0NUPev23NKkMzid3Mivbq", "credits": 0},
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
                "plan_name": request.plan_name
            },
            return_url="http://localhost:3000/workspace", 
        )
        
        return {"checkout_url": session.checkout_url}

    except Exception as e:
        logger.error(f"Payment Init Error: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/webhook")
async def dodo_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Secure Webhook Handler.
    Verifies signature and updates user credits.
    """
    try:
        # 1. Get Raw Body & Headers for Verification
        payload_bytes = await request.body()
        payload_str = payload_bytes.decode("utf-8")
        headers = request.headers

        # 2. Verify Signature (SECURITY CRITICAL)
        # Dodo uses the Standard Webhooks specification
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
        
        logger.info(f"Webhook Event: {event_type}")

        # 1. SUCCESS: Add Credits
        if event_type == "payment.succeeded":
            metadata = data.get("metadata", {})
            user_id = metadata.get("user_id")
            credits = int(metadata.get("credits_to_add", 0))

            if user_id and credits > 0:
                user = db.query(User).filter(User.id == int(user_id)).first()
                if user:
                    user.credits += credits
                    db.commit()
                    logger.info(f"SUCCESS: User {user.email} credited +{credits}")

        # 2. FAILURE: Log it for debugging
        elif event_type == "payment.failed":
            metadata = data.get("metadata", {})
            user_id = metadata.get("user_id")
            error_reason = data.get("error_message", "Unknown error")
            
            logger.warning(f"PAYMENT FAILED: User {user_id} failed to pay. Reason: {error_reason}")
            # Optional: You could trigger an email here to ask them to try again.

        # 3. REFUND: Remove Credits (Protect your business)
        elif event_type == "refund.succeeded":
            metadata = data.get("metadata", {})
            user_id = metadata.get("user_id")
            # Usually we want to remove the same amount we added
            credits_to_remove = int(metadata.get("credits_to_add", 0))

            if user_id and credits_to_remove > 0:
                user = db.query(User).filter(User.id == int(user_id)).first()
                if user:
                    # Prevent negative credits
                    user.credits = max(0, user.credits - credits_to_remove)
                    db.commit()
                    logger.info(f"REFUND PROCESSED: Removed {credits_to_remove} credits from User {user.email}")

        # 4. DISPUTE: (Optional) Handle chargebacks
        elif event_type == "dispute.opened":
             logger.critical(f"DISPUTE OPENED: Payment {data.get('payment_id')} is being disputed!")

        return {"status": "received"}

    except Exception as e:
        logger.error(f"Webhook Error: {e}")
        return {"status": "error", "detail": str(e)}