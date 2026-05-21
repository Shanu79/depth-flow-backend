import os
import json
import uuid
import requests
from datetime import datetime, timezone  # <--- Update this line
from standardwebhooks.webhooks import Webhook

# ==========================================
# 1. CONFIGURATION
# ==========================================
BASE_URL = "http://localhost:8000"
# Get a valid JWT token from a logged-in test user
ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1c2VyQGV4YW1wbGUuY29tIiwiZXhwIjoxNzc5ODIyNjg5fQ.Vv1LAMvReWsM_iLxwHewdhaEGTuGK1_ZQacAKoad5e4" 
# Your local Dodo Webhook Secret (from your .env)
WEBHOOK_SECRET = "whsec_BjJ521s4BJnSWdfMndLthomoC0SIL9Ie"

HEADERS = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "Content-Type": "application/json"
}

# ==========================================
# HELPER: MOCK WEBHOOK GENERATOR
# ==========================================
def trigger_mock_webhook(event_type: str, data_payload: dict):
    """
    Constructs a Dodo Payments webhook payload, signs it using the 
    standardwebhooks library, and sends it to your local endpoint.
    """
    print(f"\n[+] Simulating Webhook: {event_type}")
    
    payload = {
        "type": event_type,
        "data": data_payload
    }
    payload_str = json.dumps(payload)
    
    # Generate Standard Webhook Signatures
    wh = Webhook(WEBHOOK_SECRET)
    msg_id = f"msg_{uuid.uuid4()}"
    timestamp = datetime.now(timezone.utc) 
    
    # wh.sign returns ONLY the signature string
    signature = wh.sign(msg_id, timestamp, payload_str)
    
    # Manually construct the required standard webhook headers
    webhook_headers = {
        "Content-Type": "application/json",
        "webhook-id": msg_id,
        "webhook-timestamp": str(int(timestamp.timestamp())),
        "webhook-signature": signature
    }
    
    # Send to the local backend
    response = requests.post(
        f"{BASE_URL}/payments/webhook",
        headers=webhook_headers,
        data=payload_str
    )
    
    print(f"    -> Response [{response.status_code}]: {response.json()}")
    return response.json()

def get_user_state():
    """Fetches the current user's DB state (Credits, Plan, Sub ID)"""
    res = requests.get(f"{BASE_URL}/auth/me", headers=HEADERS)
    return res.json()

# ==========================================
# TEST SUITE
# ==========================================
def run_tests():
    user = get_user_state()
    user_id = user.get("id")
    user_email = user.get("email")
    print(f"--- Starting Tests for User: {user_email} ---")
    print(f"Initial State: {user['credits']} Credits | Plan: {user['plan']}")

    # ---------------------------------------------------------
    # TEST 1: NEW SUBSCRIPTION CHECKOUT
    # ---------------------------------------------------------
    print("\n--- TEST 1: Checkout Session (New Pro Plan) ---")
    res = requests.post(
        f"{BASE_URL}/payments/create-checkout-session",
        headers=HEADERS,
        json={"plan_name": "Pro", "billing_cycle": "monthly", "quantity": 1}
    )
    print(f"Response: {res.json()}")

    # ---------------------------------------------------------
    # TEST 2: SIMULATE DODO 'PAYMENT SUCCEEDED' (NEW SUB)
    # ---------------------------------------------------------
    sub_1_id = f"sub_test_{uuid.uuid4().hex[:8]}"
    trigger_mock_webhook("payment.succeeded", {
        "subscription_id": sub_1_id,
        "customer": {"email": user_email},
        "metadata": {
            "user_id": str(user_id),
            "credits_to_add": "1500",
            "plan_name": "Pro",
            "billing_cycle": "monthly",
            "is_one_time": "false",
            "is_api": "false",
            "old_sub_id": ""
        }
    })
    
    user = get_user_state()
    print(f"State after New Sub: {user['credits']} Credits | Plan: {user['plan']} | Sub ID: {user['subscription_id']}")

    # ---------------------------------------------------------
    # TEST 3: UPGRADE PLAN CHECKOUT
    # ---------------------------------------------------------
    print("\n--- TEST 3: Checkout Session (Upgrade to Premium) ---")
    res = requests.post(
        f"{BASE_URL}/payments/create-checkout-session",
        headers=HEADERS,
        json={"plan_name": "Premium", "billing_cycle": "monthly", "quantity": 1}
    )
    print(f"Response: {res.json()}")

    # ---------------------------------------------------------
    # TEST 4: SIMULATE UPGRADE PAYMENT (Rollover Credits)
    # ---------------------------------------------------------
    sub_2_id = f"sub_test_{uuid.uuid4().hex[:8]}"
    trigger_mock_webhook("payment.succeeded", {
        "subscription_id": sub_2_id,
        "customer": {"email": user_email},
        "metadata": {
            "user_id": str(user_id),
            "credits_to_add": "4000",
            "plan_name": "Premium",
            "billing_cycle": "monthly",
            "is_one_time": "false",
            "is_api": "false",
            "old_sub_id": sub_1_id # Triggers the immediate cancel of the old sub
        }
    })

    user = get_user_state()
    print(f"State after Upgrade: {user['credits']} Credits | Plan: {user['plan']} | Sub ID: {user['subscription_id']}")

    # ---------------------------------------------------------
    # TEST 5: OLD SUBSCRIPTION CANCELLATION (Webhook Ignored)
    # ---------------------------------------------------------
    print("\n--- TEST 5: Late Cancel Webhook for Old Sub (Should NOT zero credits) ---")
    trigger_mock_webhook("subscription.cancelled", {
        "subscription_id": sub_1_id,
        "customer": {"email": user_email}
    })
    
    user = get_user_state()
    print(f"State after Old Sub Cancel: {user['credits']} Credits (Should match Upgrade credits)")

    # ---------------------------------------------------------
    # TEST 6: AUTO-RENEWAL (Monthly/Yearly Credit Addition)
    # ---------------------------------------------------------
    print("\n--- TEST 6: Auto-Renewal for Active Sub (Testing `is_renewal` logic) ---")
    # Missing metadata mimics an auto-renewal where Dodo doesn't attach checkout metadata
    trigger_mock_webhook("payment.succeeded", {
        "subscription_id": sub_2_id, # Matches the current active sub ID
        "customer": {"email": user_email}
    })
    
    user = get_user_state()
    print(f"State after Auto-Renewal: {user['credits']} Credits (Should have added another 4000)")

    # ---------------------------------------------------------
    # TEST 7: USER INITIATED CANCELLATION
    # ---------------------------------------------------------
    print("\n--- TEST 7: User Calls API to Cancel ---")
    res = requests.post(
        f"{BASE_URL}/payments/cancel-subscription",
        headers=HEADERS,
        json={"plan_type": "workspace"}
    )
    print(f"Response: {res.json()}")
    
    # ---------------------------------------------------------
    # TEST 8: SWITCH TO YEARLY PLAN (DRIP FEED)
    # ---------------------------------------------------------
    print("\n--- TEST 8: User switches to Yearly Pro Plan ---")
    sub_yearly_id = f"sub_test_{uuid.uuid4().hex[:8]}"
    
    # Simulating the checkout metadata for a Yearly Pro plan
    trigger_mock_webhook("payment.succeeded", {
        "subscription_id": sub_yearly_id,
        "customer": {"email": user_email},
        "metadata": {
            "user_id": str(user_id),
            "credits_to_add": "18000", # The webhook should now intercept this and divide by 12
            "plan_name": "Pro",
            "billing_cycle": "yearly",
            "is_one_time": "false",
            "is_api": "false",
            "old_sub_id": "sub_test_0bc73c8b" 
        }
    })

    user = get_user_state()
    # Expectation changed: 18000 / 12 = 1500
    print(f"State after Yearly Upgrade: {user['credits']} Credits (Should be 1500 - 1/12th drop) | Plan: {user['plan']} | Cycle: {user['billing_cycle']}")

    # ---------------------------------------------------------
    # TEST 9: YEARLY AUTO-RENEWAL (1 Year Later)
    # ---------------------------------------------------------
    print("\n--- TEST 9: Auto-Renewal for Yearly Sub (1 Year Later) ---")
    # Mimicking an auto-renewal where Dodo hits the webhook.
    trigger_mock_webhook("payment.succeeded", {
        "subscription_id": sub_yearly_id, 
        "customer": {"email": user_email}
    })
    
    user = get_user_state()
    # The renewal resets the cycle, dropping the first month of the new year
    print(f"State after Yearly Auto-Renewal: {user['credits']} Credits (Should be reset to 1500)")
    # ---------------------------------------------------------
    # TEST 10: BUY ADD-ON CREDIT PACK
    # ---------------------------------------------------------
    print("\n--- TEST 10: User buys a One-Time Credit Pack ---")
    # One-time purchases have a transaction ID, but not a recurring sub ID
    trigger_mock_webhook("payment.succeeded", {
        "subscription_id": f"tx_test_{uuid.uuid4().hex[:8]}", 
        "customer": {"email": user_email},
        "metadata": {
            "user_id": str(user_id),
            "credits_to_add": "900",
            "plan_name": "Credit Pack",
            "billing_cycle": "one_time",
            "is_one_time": "true",
            "is_api": "false",
            "old_sub_id": ""
        }
    })
    
    user = get_user_state()
    print(f"State after Add-on: {user['credits']} Credits (Should have added 900 to the pile)")

   # ---------------------------------------------------------
    # TEST 11: THE EXPIRATION EVENT (Auto-Renewal)
    # ---------------------------------------------------------
    print("\n--- TEST 11: Auto-Renewal (Testing Credit Expiration) ---")
    print("-> This should wipe out the old credits and the add-on, resetting to the monthly drop limit.")
    
    trigger_mock_webhook("payment.succeeded", {
        "subscription_id": sub_yearly_id, 
        "customer": {"email": user_email}
    })
    
    user = get_user_state()
    print(f"State after Expiration/Renewal: {user['credits']} Credits (Should be EXACTLY 1500)")

    # ---------------------------------------------------------
    # TEST 12: WEBHOOK IDEMPOTENCY (Prevent Double Credits)
    # ---------------------------------------------------------
    print("\n--- TEST 12: Webhook Idempotency (Prevent Double Credits) ---")
    duplicate_payment_id = f"pay_test_{uuid.uuid4().hex[:8]}"
    
    print("-> Sending first webhook (Buying 500 credits)...")
    trigger_mock_webhook("payment.succeeded", {
        "payment_id": duplicate_payment_id,  # Notice we are explicitly passing a payment_id
        "subscription_id": f"tx_test_{uuid.uuid4().hex[:8]}", 
        "customer": {"email": user_email},
        "metadata": {
            "user_id": str(user_id),
            "credits_to_add": "500",
            "plan_name": "Credit Pack",
            "billing_cycle": "one_time",
            "is_one_time": "true",
            "is_api": "false"
        }
    })
    
    user_after_first = get_user_state()
    expected_credits = user_after_first['credits']
    print(f"State after first webhook: {expected_credits} Credits")

    print("\n-> Sending the EXACT SAME webhook again (Simulating network glitch)...")
    res_duplicate = trigger_mock_webhook("payment.succeeded", {
        "payment_id": duplicate_payment_id,  # EXACT SAME PAYMENT ID
        "subscription_id": f"tx_test_{uuid.uuid4().hex[:8]}", 
        "customer": {"email": user_email},
        "metadata": {
            "user_id": str(user_id),
            "credits_to_add": "500",
            "plan_name": "Credit Pack",
            "billing_cycle": "one_time",
            "is_one_time": "true",
            "is_api": "false"
        }
    })
    
    user_after_second = get_user_state()
    print(f"State after duplicate webhook: {user_after_second['credits']} Credits (Should STILL be exactly {expected_credits})")
    
    if res_duplicate.get("status") == "ignored":
        print("✅ SUCCESS: Duplicate webhook was caught and ignored!")
    else:
        print("❌ FAILED: Duplicate webhook was processed.")

if __name__ == "__main__":
    if ACCESS_TOKEN == "YOUR_BEARER_TOKEN_HERE":
        print("ERROR: Please set your ACCESS_TOKEN and WEBHOOK_SECRET in the script.")
    else:
        run_tests()