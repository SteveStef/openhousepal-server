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
        old_tier = user.plan_tier
        now = datetime.now(timezone.utc)

        # 1. Dates first (critical for grace periods and history)
        if next_billing_date:
            if not user.next_billing_date or abs((user.next_billing_date - next_billing_date).total_seconds()) > 3600:
                user.next_billing_date = next_billing_date
                needs_update = True

        if last_payment_date:
            if not user.last_billing_date or abs((user.last_billing_date - last_payment_date).total_seconds()) > 3600:
                user.last_billing_date = last_payment_date
                needs_update = True

        trial_running = bool(user.trial_ends_at and user.trial_ends_at > now)

        # 2. Status - PayPal is the source of truth. This is the safety net for a webhook that
        # never arrived (cancelled / paused / reinstated in PayPal, or a trial that converted).
        if paypal_status == "ACTIVE":
            want = "TRIAL" if trial_running else "ACTIVE"
            if user.subscription_status != want:
                user.subscription_status = want
                if want == "ACTIVE" and user.trial_ends_at and not trial_running:
                    user.trial_ends_at = None
                needs_update = True
        elif paypal_status == "SUSPENDED":
            if user.subscription_status != "SUSPENDED":
                user.subscription_status = "SUSPENDED"
                needs_update = True
        elif paypal_status == "CANCELLED":
            if user.subscription_status == "TRIAL" and not trial_running:
                user.subscription_status = "EXPIRED"  # trial ended without converting
                needs_update = True
            elif user.subscription_status in ("TRIAL", "ACTIVE", "SUSPENDED"):
                user.subscription_status = "CANCELLED"
                # Keep access to the end of what they already had (PayPal drops
                # next_billing_time once a subscription is cancelled).
                if not (user.next_billing_date and user.next_billing_date > now) and not trial_running:
                    base = user.last_billing_date or user.subscription_started_at or now
                    user.next_billing_date = max(now, base + timedelta(days=30)) if user.last_billing_date else now
                needs_update = True
        elif paypal_status == "EXPIRED":
            if user.subscription_status != "EXPIRED":
                user.subscription_status = "EXPIRED"
                needs_update = True

        # 3. Plan - an approved upgrade/downgrade whose webhook never came. PayPal only switches
        # plan_id once the agent approved the change, so this never jumps ahead of them.
        if paypal_status == "ACTIVE" and paypal_plan_id and paypal_plan_id != user.plan_id:
            from app.utils.plan_ids import tier_for_plan_id
            new_tier = tier_for_plan_id(paypal_plan_id)
            if not new_tier:
                from app.services.promo_code_service import tier_for_promo_plan
                new_tier = (await tier_for_promo_plan(db, paypal_plan_id))[0]
            if new_tier:
                from app.services.website.site_service import reset_idx_site_question_on_upgrade
                reset_idx_site_question_on_upgrade(user, user.plan_tier, new_tier)
                user.plan_tier = new_tier
                user.plan_id = paypal_plan_id
                needs_update = True

        if needs_update:
            logger.info(
                f"Synced subscription for user {user.id}",
                extra={
                    "old_status": old_status,
                    "new_status": user.subscription_status,
                    "paypal_status": paypal_status,
                    "old_tier": old_tier,
                    "new_tier": user.plan_tier,
                }
            )

        # Always update the sync timestamp so we know we checked
        user.last_paypal_sync = now
        await db.commit()

        if user.plan_tier != old_tier:
            # Landing on a Showcase plan: send the showcases they missed (no-op otherwise).
            try:
                from app.services.open_house_service import schedule_showcase_backfill
                schedule_showcase_backfill(user.id)
            except Exception:
                logger.warning("showcase backfill after sync failed", exc_info=True)

        return True
            
    except Exception as e:
        logger.error(f"Failed to sync subscription for user {user.id}", exc_info=True)
        return False
