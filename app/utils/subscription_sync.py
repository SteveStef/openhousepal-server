from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from datetime import datetime, timezone, timedelta
from app.models.database import User
from app.services.paypal_service import paypal_service
from app.config.logging import get_logger
import os

logger = get_logger(__name__)

async def sync_subscription_status(user: User, db: AsyncSession) -> bool:
    """
    Syncs a single user's subscription status with PayPal.
    Returns True if the status was updated, False otherwise.
    
    This function:
    1. Fetches the latest subscription details from PayPal
    2. Compares with local database state
    3. Updates local state if there's a discrepancy
    4. Calculates and updates grace periods (next_billing_date)
    """
    if not user.subscription_id:
        return False
        
    try:
        # Fetch latest details from PayPal
        sub_details = await paypal_service.get_subscription(user.subscription_id)
        
        paypal_status = sub_details.get("status")
        paypal_plan_id = sub_details.get("plan_id")
        
        billing_info = sub_details.get("billing_info", {})
        next_billing_time_str = billing_info.get("next_billing_time")
        last_payment = billing_info.get("last_payment", {})
        last_payment_time_str = last_payment.get("time")
        
        # Parse dates if available
        next_billing_date = None
        if next_billing_time_str:
            try:
                next_billing_date = datetime.fromisoformat(next_billing_time_str.replace('Z', '+00:00'))
            except Exception:
                pass

        last_payment_date = None
        if last_payment_time_str:
            try:
                last_payment_date = datetime.fromisoformat(last_payment_time_str.replace('Z', '+00:00'))
            except Exception:
                pass
                
        # Determine if we need to update
        needs_update = False
        old_status = user.subscription_status
        
        # 1. Check Status Mismatch
        # ... (rest of logic) ...
        
        # 3. Update Dates (Critical for grace periods and history)
        if next_billing_date:
            if not user.next_billing_date or abs((user.next_billing_date - next_billing_date).total_seconds()) > 3600:
                user.next_billing_date = next_billing_date
                needs_update = True
        
        if last_payment_date:
            if not user.last_billing_date or abs((user.last_billing_date - last_payment_date).total_seconds()) > 3600:
                user.last_billing_date = last_payment_date
                needs_update = True
                
        # 4. Check for infinite trial loophole
        # If local status is TRIAL but trial_ends_at is in the past, force expiration check
        now = datetime.now(timezone.utc)
        if user.subscription_status == "TRIAL" and user.trial_ends_at and user.trial_ends_at < now:
            # Trial expired locally. 
            # If PayPal says ACTIVE, they converted.
            if paypal_status == "ACTIVE":
                user.subscription_status = "ACTIVE"
                user.trial_ends_at = None # Clear trial
                needs_update = True
            # If PayPal says CANCELLED/EXPIRED, they didn't convert
            elif paypal_status in ["CANCELLED", "EXPIRED", "SUSPENDED"]:
                user.subscription_status = "EXPIRED"
                needs_update = True
                
        if needs_update:
            logger.info(
                f"Synced subscription for user {user.id}",
                extra={
                    "old_status": old_status,
                    "new_status": user.subscription_status,
                    "paypal_status": paypal_status
                }
            )

        # Always update the sync timestamp so we know we checked
        user.last_paypal_sync = now
        await db.commit()
        
        return True
            
    except Exception as e:
        logger.error(f"Failed to sync subscription for user {user.id}", exc_info=True)
        return False
