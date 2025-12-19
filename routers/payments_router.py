import os
from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel
from typing import Optional
from dodopayments import DodoPayments

router = APIRouter(
    prefix="/payments",
    tags=["payments"]
)

client = DodoPayments(
    bearer_token=os.environ.get("DODO_PAYMENTS_API_KEY_TEST"),
    environment="test_mode" 
)

class CheckoutRequest(BaseModel):
    plan_name: str
    billing_cycle: str
    quantity: int = 1

# Mock auth verification (Replace with your actual one)
async def verify_token(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid token format")
    return True

@router.post("/create-checkout-session")
async def create_checkout_session(
    request: CheckoutRequest, 
    authorized: bool = Depends(verify_token)
):
    try:
        # 1. Map your plan names to Dodo Product IDs
        # REPLACE THESE with your actual Product IDs from Dodo Dashboard
        product_map = {
            "monthly": {
                "Basic": "pdt_0NUPe15y0JO1bh0nqtE9e", 
                "Pro": "pdt_0NUPeVOcEvNPCNYVV3LgA",
                "Free": "pdt_0NUPev23NKkMzid3Mivbq"
            },
            "yearly": {
                "Basic": "pdt_0NUPhhIR8EDmFZhXlEXU4",
                "Pro": "pdt_0NUPhrqLTrV4gHrXi5CoD",
                "Free": "pdt_0NUPev23NKkMzid3Mivbq"
            }
        }
        
        # 1. Select the correct cycle dictionary (default to monthly if invalid)
        cycle_products = product_map.get(request.billing_cycle, product_map["monthly"])

        product_id = cycle_products.get(request.plan_name)
        if not product_id:
            raise HTTPException(status_code=400, detail=f"Invalid plan name: {request.plan_name}")

        # 2. Create the Checkout Session
        # REMOVED 'billing' argument to fix the error
        session = client.checkout_sessions.create(
            product_cart=[{
                "product_id": product_id,
                "quantity": request.quantity
            }],
            customer={
                # In a real app, fetch this from your database using the token
                "email": "customer@example.com", 
                "name": "John Doe"
            },
            return_url="http://localhost:3000/workspace", 
        )
        
        # 3. Return the URL for the Overlay
        return {"checkout_url": session.checkout_url}

    except Exception as e:
        print(f"Payment Error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))