from fastapi import APIRouter, Request, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timezone, timedelta
import os

from app.database import get_db
from app.models.database import User, WebhookEvent
from app.services.paypal_service import paypal_service
from app.config.logging import get_logger

router = APIRouter()
logger = get_logger(__name__)

# PayPal Plan ID Constants
BASIC_PLAN_ID = os.getenv("PAYPAL_BASIC_PLAN_ID")
PREMIUM_PLAN_ID = os.getenv("PAYPAL_PREMIUM_PLAN_ID")
BASIC_PLAN_NO_TRIAL_ID = os.getenv("PAYPAL_BASIC_NO_TRIAL_PLAN_ID")
PREMIUM_PLAN_NO_TRIAL_ID = os.getenv("PAYPAL_PREMIUM_NO_TRIAL_PLAN_ID")
PAYPAL_WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID")

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
        # Extract webhook headers for signature verification
        transmission_id = request.headers.get("paypal-transmission-id")
        transmission_time = request.headers.get("paypal-transmission-time")
        cert_url = request.headers.get("paypal-cert-url")
        auth_algo = request.headers.get("paypal-auth-algo")
        transmission_sig = request.headers.get("paypal-transmission-sig")

        # Get the webhook body
        body = await request.json()

        # Verify webhook signature BEFORE processing anything
        if not all([transmission_id, transmission_time, cert_url, auth_algo, transmission_sig, PAYPAL_WEBHOOK_ID]):
            logger.error("SECURITY: Missing webhook verification headers or webhook ID")
            raise HTTPException(status_code=400, detail="Missing required webhook headers")

        try:
            is_valid = await paypal_service.verify_webhook_signature(
                transmission_id=transmission_id,
                transmission_time=transmission_time,
                cert_url=cert_url,
                auth_algo=auth_algo,
                transmission_sig=transmission_sig,
                webhook_id=PAYPAL_WEBHOOK_ID,
                webhook_event=body
            )

            if not is_valid:
                logger.error("SECURITY: Invalid webhook signature detected", extra={"transmission_id": transmission_id})
                raise HTTPException(status_code=401, detail="Invalid webhook signature")

            logger.info("Webhook signature verified", extra={"transmission_id": transmission_id})

        except HTTPException:
            raise
        except Exception as e:
            logger.error("SECURITY: Webhook verification failed", exc_info=True)
            raise HTTPException(status_code=401, detail="Webhook verification failed")

        event_id = body.get('id')
        event_type = body.get("event_type")
        logger.info("PayPal webhook received", extra={"event_type": event_type, "event_id": event_id})

        if not event_id or not event_type:
            logger.warning("Webhook missing event_id or event_type")
            raise HTTPException(status_code=400, detail="Missing event ID or event_type")

        existing = await db.execute(select(WebhookEvent).where(WebhookEvent.id == event_id))
        if existing.scalar_one_or_none():
            logger.info("Webhook event already processed, skipping", extra={"event_id": event_id})
            return {"status": "already_processed", "event_id": event_id}

        # Extract subscription ID from different possible locations
        resource = body.get("resource", {})
        subscription_id = resource.get("id") or resource.get("billing_agreement_id")

        if not subscription_id:
            logger.warning("Webhook missing subscription_id", extra={"event_type": event_type})
            return {"received": True, "warning": "No subscription_id"}

        # Find user by subscription_id
        result = await db.execute(
            select(User).where(User.subscription_id == subscription_id)
        )
        user = result.scalar_one_or_none()

        if not user:
            logger.warning("User not found for subscription", extra={"subscription_id": subscription_id})
            return {"received": True, "warning": "User not found"}

        logger.info("Processing webhook for user", extra={"subscription_id": subscription_id, "user_id": user.id})

        # Handle different event types
        if event_type == "BILLING.SUBSCRIPTION.ACTIVATED":
            # Trial converted to paid, or subscription reactivated
            user.subscription_status = "ACTIVE"
            if not user.subscription_started_at:
                user.subscription_started_at = datetime.now(timezone.utc)
            user.last_paypal_sync = datetime.now(timezone.utc)
            logger.info("Subscription activated", extra={"subscription_id": subscription_id, "user_id": user.id})

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
                    logger.info(
                        "Subscription cancelled with grace period",
                        extra={
                            "subscription_id": subscription_id,
                            "user_id": user.id,
                            "grace_until": next_billing_time
                        }
                    )
                except Exception as e:
                    logger.warning("Failed to parse next_billing_time", exc_info=True)
            else:
                # Fallback: Calculate grace period manually if PayPal doesn't provide it
                if user.last_billing_date:
                    # User was billed recently - add 30 days from last billing
                    user.next_billing_date = user.last_billing_date + timedelta(days=30)
                    grace_source = "last_billing_date"
                elif user.subscription_started_at:
                    # Calculate from subscription start date + 30 days
                    user.next_billing_date = user.subscription_started_at + timedelta(days=30)
                    grace_source = "subscription_started_at"
                else:
                    # Safety fallback: Give 30 days from now
                    user.next_billing_date = datetime.now(timezone.utc) + timedelta(days=30)
                    grace_source = "current_time"

                logger.info(
                    "Subscription cancelled with calculated grace period",
                    extra={
                        "subscription_id": subscription_id,
                        "user_id": user.id,
                        "grace_until": user.next_billing_date.isoformat(),
                        "grace_calculated_from": grace_source
                    }
                )

        elif event_type == "BILLING.SUBSCRIPTION.SUSPENDED":
            # Payment failed - subscription suspended
            user.subscription_status = "SUSPENDED"
            user.last_paypal_sync = datetime.now(timezone.utc)
            logger.warning("Subscription suspended due to payment failure", extra={"subscription_id": subscription_id, "user_id": user.id})

        elif event_type == "BILLING.SUBSCRIPTION.EXPIRED":
            # Subscription ended (after cancellation grace period)
            user.subscription_status = "EXPIRED"
            user.last_paypal_sync = datetime.now(timezone.utc)
            logger.info("Subscription expired", extra={"subscription_id": subscription_id, "user_id": user.id})

        elif event_type == "BILLING.SUBSCRIPTION.UPDATED":
            # Plan changed (upgrade/downgrade)
            new_plan_id = resource.get("plan_id")

            if new_plan_id:
                # Determine tier based on plan_id (handle both trial and no-trial plans)
                if new_plan_id in [BASIC_PLAN_ID, BASIC_PLAN_NO_TRIAL_ID]:
                    user.plan_tier = "BASIC"
                    new_tier = "BASIC"
                elif new_plan_id in [PREMIUM_PLAN_ID, PREMIUM_PLAN_NO_TRIAL_ID]:
                    user.plan_tier = "PREMIUM"
                    new_tier = "PREMIUM"
                else:
                    new_tier = "UNKNOWN"

                logger.info(
                    "Subscription plan updated",
                    extra={
                        "subscription_id": subscription_id,
                        "user_id": user.id,
                        "new_tier": new_tier,
                        "new_plan_id": new_plan_id
                    }
                )

                user.plan_id = new_plan_id

                # Important: Don't modify trial_ends_at here - preserve existing trial period
                # If user is switching plans during trial, they keep the same trial end date

            user.last_paypal_sync = datetime.now(timezone.utc)

        elif event_type == "PAYMENT.SALE.COMPLETED":
            # Recurring payment succeeded
            user.last_billing_date = datetime.now(timezone.utc)
            user.subscription_status = "ACTIVE"  # Ensure it's active
            user.last_paypal_sync = datetime.now(timezone.utc)
            logger.info("Payment completed", extra={"subscription_id": subscription_id, "user_id": user.id})

        elif event_type in ["PAYMENT.SALE.DENIED", "PAYMENT.SALE.REFUNDED"]:
            # Payment failed or refunded
            user.subscription_status = "SUSPENDED"
            user.last_paypal_sync = datetime.now(timezone.utc)
            logger.warning("Payment issue", extra={"subscription_id": subscription_id, "user_id": user.id, "issue_type": event_type})

        else:
            # Unknown event type - log it but don't fail
            logger.info("Unhandled webhook event type", extra={"event_type": event_type})
            return {"received": True, "event_type": event_type, "message": "Event type not handled"}

        # Record that we processed this event (for idempotency)
        webhook_record = WebhookEvent(
            id=event_id,
            event_type=event_type
        )
        db.add(webhook_record)

        # Save changes to database (both user updates and webhook event record)
        await db.commit()

        logger.info("Webhook processed successfully", extra={"user_id": user.id, "event_type": event_type})

        return {
            "received": True,
            "event_type": event_type,
            "subscription_id": subscription_id,
            "user_email": user.email,
            "new_status": user.subscription_status
        }

    except Exception as e:
        logger.error("Webhook processing error", exc_info=True)
        # Return 200 anyway so PayPal doesn't retry indefinitely
        return {"received": True, "error": str(e)}
