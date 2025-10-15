from fastapi import APIRouter, Request, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timezone
import os

from app.database import get_db
from app.models.database import User

router = APIRouter()

# PayPal Plan ID Constants
BASIC_PLAN_ID = os.getenv("PAYPAL_BASIC_PLAN_ID")
PREMIUM_PLAN_ID = os.getenv("PAYPAL_PREMIUM_PLAN_ID")

@router.post("/webhooks/paypal")
async def handle_paypal_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Receive and process PayPal webhook events.

    Handles subscription lifecycle events:
    - BILLING.SUBSCRIPTION.ACTIVATED - Trial converted to paid
    - BILLING.SUBSCRIPTION.CANCELLED - User cancelled
    - BILLING.SUBSCRIPTION.SUSPENDED - Payment failed
    - BILLING.SUBSCRIPTION.EXPIRED - Subscription ended
    - BILLING.SUBSCRIPTION.UPDATED - Plan changed
    - PAYMENT.SALE.COMPLETED - Recurring payment succeeded
    - PAYMENT.SALE.DENIED - Payment failed
    """
    try:
        # Get the raw body
        body = await request.json()
        event_type = body.get("event_type")

        print(f"📥 PayPal Webhook Received: {event_type}")

        # Extract subscription ID from different possible locations
        resource = body.get("resource", {})
        subscription_id = resource.get("id") or resource.get("billing_agreement_id")

        if not subscription_id:
            print("⚠️ No subscription_id found in webhook payload")
            return {"received": True, "warning": "No subscription_id"}

        # Find user by subscription_id
        result = await db.execute(
            select(User).where(User.subscription_id == subscription_id)
        )
        user = result.scalar_one_or_none()

        if not user:
            print(f"⚠️ No user found for subscription {subscription_id}")
            return {"received": True, "warning": "User not found"}

        print(f"👤 Processing webhook for user: {user.email}")

        # Handle different event types
        if event_type == "BILLING.SUBSCRIPTION.ACTIVATED":
            # Trial converted to paid, or subscription reactivated
            user.subscription_status = "ACTIVE"
            if not user.subscription_started_at:
                user.subscription_started_at = datetime.now(timezone.utc)
            user.last_paypal_sync = datetime.now(timezone.utc)
            print(f"✅ Subscription {subscription_id} activated for {user.email}")

        elif event_type == "BILLING.SUBSCRIPTION.CANCELLED":
            # User cancelled subscription
            user.subscription_status = "CANCELLED"
            user.last_paypal_sync = datetime.now(timezone.utc)

            # Extract next_billing_date for grace period
            billing_info = resource.get("billing_info", {})
            next_billing_time = billing_info.get("next_billing_time")

            if next_billing_time:
                try:
                    # Parse ISO 8601 datetime from PayPal
                    user.next_billing_date = datetime.fromisoformat(next_billing_time.replace('Z', '+00:00'))
                    print(f"❌ Subscription {subscription_id} cancelled for {user.email} (grace until {next_billing_time})")
                except Exception as e:
                    print(f"⚠️ Failed to parse next_billing_time: {e}")
            else:
                print(f"❌ Subscription {subscription_id} cancelled for {user.email} (no grace period date)")

        elif event_type == "BILLING.SUBSCRIPTION.SUSPENDED":
            # Payment failed - subscription suspended
            user.subscription_status = "SUSPENDED"
            user.last_paypal_sync = datetime.now(timezone.utc)
            print(f"⏸️ Subscription {subscription_id} suspended for {user.email} (payment failed)")

        elif event_type == "BILLING.SUBSCRIPTION.EXPIRED":
            # Subscription ended (after cancellation grace period)
            user.subscription_status = "EXPIRED"
            user.last_paypal_sync = datetime.now(timezone.utc)
            print(f"⏹️ Subscription {subscription_id} expired for {user.email}")

        elif event_type == "BILLING.SUBSCRIPTION.UPDATED":
            # Plan changed (upgrade/downgrade)
            new_plan_id = resource.get("plan_id")

            if new_plan_id:
                # Determine tier based on plan_id
                if new_plan_id == BASIC_PLAN_ID:
                    user.plan_tier = "BASIC"
                    print(f"🔄 Subscription {subscription_id} downgraded to BASIC for {user.email}")
                elif new_plan_id == PREMIUM_PLAN_ID:
                    user.plan_tier = "PREMIUM"
                    print(f"🔄 Subscription {subscription_id} upgraded to PREMIUM for {user.email}")

                user.plan_id = new_plan_id

            user.last_paypal_sync = datetime.now(timezone.utc)

        elif event_type == "PAYMENT.SALE.COMPLETED":
            # Recurring payment succeeded
            user.last_billing_date = datetime.now(timezone.utc)
            user.subscription_status = "ACTIVE"  # Ensure it's active
            user.last_paypal_sync = datetime.now(timezone.utc)
            print(f"💰 Payment completed for subscription {subscription_id} ({user.email})")

        elif event_type in ["PAYMENT.SALE.DENIED", "PAYMENT.SALE.REFUNDED"]:
            # Payment failed or refunded
            user.subscription_status = "SUSPENDED"
            user.last_paypal_sync = datetime.now(timezone.utc)
            print(f"⚠️ Payment issue for subscription {subscription_id} ({user.email}): {event_type}")

        else:
            # Unknown event type - log it but don't fail
            print(f"ℹ️ Unhandled webhook event: {event_type}")
            return {"received": True, "event_type": event_type, "message": "Event type not handled"}

        # Save changes to database
        await db.commit()

        print(f"✅ Webhook processed successfully for {user.email}")

        return {
            "received": True,
            "event_type": event_type,
            "subscription_id": subscription_id,
            "user_email": user.email,
            "new_status": user.subscription_status
        }

    except Exception as e:
        print(f"❌ Webhook processing error: {e}")
        # Return 200 anyway so PayPal doesn't retry indefinitely
        return {"received": True, "error": str(e)}
