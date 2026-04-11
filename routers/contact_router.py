# routers/contact_router.py
import logging
from fastapi import APIRouter
from pydantic import BaseModel, EmailStr

# Setup logging so you can see the messages in your terminal
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/contact", tags=["contact"])

# Define the expected JSON payload from React
class ContactForm(BaseModel):
    email: EmailStr
    subject: str
    message: str

@router.post("")
async def handle_contact_submission(form: ContactForm):
    """
    Receives contact form submissions from the frontend.
    """
    # 1. Log the message to your terminal
    logger.info("=" * 40)
    logger.info("📩 NEW SUPPORT MESSAGE RECEIVED")
    logger.info("=" * 40)
    logger.info(f"From:    {form.email}")
    logger.info(f"Subject: {form.subject}")
    logger.info(f"Message: {form.message}")
    logger.info("=" * 40)
    
    # 2. TODO: Add Email Sending Logic Here
    # (e.g., Use smtplib, SendGrid, Amazon SES, or Resend to forward 
    # this data to your api@depthflow.io email address)

    return {
        "status": "success", 
        "message": "Your message has been received. Our support team will contact you shortly."
    }