from fastapi import APIRouter, HTTPException, Depends, status, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timezone, timedelta
from pydantic import BaseModel
from typing import Optional
import os

from app.database import get_db
from app.schemas.user import User
from app.services.paypal_service import paypal_service
from app.utils.auth import get_current_active_user
from app.models.database import User as UserModel, PlanTier
from app.utils.plan_ids import (
    tier_for_plan_id, plan_id_has_trial, CURRENT_NO_TRIAL_PLAN_ID_BY_TIER, display_name_for_tier,
    tier_allowed_for, NOT_IN_BRIGHT_MESSAGE,
)
from app.config.logging import get_logger

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])
logger = get_logger(__name__)

# Legacy Basic/Premium plan ID constants - still needed here only for /complete-new's has_trial
# check (a legacy trial-bearing signup is still possible until those plan IDs are retired).
# All new-signup/upgrade/downgrade plan selection goes through CURRENT_NO_TRIAL_PLAN_ID_BY_TIER
# instead - see app.utils.plan_ids.
BASIC_PLAN_ID = os.getenv("PAYPAL_BASIC_PLAN_ID")
PREMIUM_PLAN_ID = os.getenv("PAYPAL_PREMIUM_PLAN_ID")
BASIC_PLAN_NO_TRIAL_ID = os.getenv("PAYPAL_BASIC_NO_TRIAL_PLAN_ID")
PREMIUM_PLAN_NO_TRIAL_ID = os.getenv("PAYPAL_PREMIUM_NO_TRIAL_PLAN_ID")

CLIENT_URL = os.getenv("CLIENT_URL", "http://localhost:3000")


class ChangePlanRequest(BaseModel):
    target_tier: str  # NO_MLS | BASIC | PREMIUM | IDX
    code: Optional[str] = None  # waives the one-time IDX setup fee


IDX_SETUP_FEE_PLAN_ID = os.getenv("PAYPAL_IDX_PLAN_ID")  # IDX with the one-time setup fee


def _owes_idx_setup_fee(user, target_tier: str) -> bool:
    return target_tier == "IDX" and user.plan_tier != "IDX" and not getattr(user, "idx_setup_settled_at", None)


async def _apply_idx_waiver(db, user_id: str, code: str):
    """Check + count an IDX fee-waiver code for this agent. Returns the code's PayPal plan id."""
    from app.services.promo_code_service import claim, idx_waiver
    promo, why = await idx_waiver(db, code)
    if promo is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=why)
    if not await claim(db, promo.id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That code was just used up. Please contact us.")
    u = (await db.execute(select(UserModel).where(UserModel.id == user_id))).scalar_one()
    u.promo_code_id = promo.id
    u.idx_setup_settled_at = datetime.now(timezone.utc)
    if promo.billing_group_id:
        u.billing_group_id = promo.billing_group_id
    await db.commit()
    return promo.paypal_plan_id


@router.get("/impact")
async def get_plan_change_impact(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """The agent's live, client-facing footprint - what's actually riding on their plan right now.
    Settings > Billing combines these counts with the tier-feature matrix to show a concrete,
    specific warning before a downgrade or cancel ("12 clients will stop getting alerts", "your
    3 forms' QR codes stop working"), instead of a generic "features will be disabled"."""
    from sqlalchemy import func
    from app.models.database import (
        AgentForm, AgentHub, AgentReview, Blog, CalendarConnection, Collection, DesignProject,
        EmailSignature, OpenHouseEvent, PropertyWebsite, Site, StudioMediaAsset,
    )

    async def count(stmt) -> int:
        return int((await db.execute(stmt)).scalar() or 0)

    uid = current_user.id
    now = datetime.now(timezone.utc)
    hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == uid))).scalar_one_or_none()
    site = (await db.execute(select(Site).where(Site.owner_id == uid))).scalar_one_or_none()
    return {
        "active_showcases": await count(select(func.count(Collection.id)).where(Collection.owner_id == uid, Collection.status == "ACTIVE")),
        "showcase_clients": await count(select(func.count(func.distinct(func.lower(Collection.visitor_email)))).where(
            Collection.owner_id == uid, Collection.status == "ACTIVE", Collection.visitor_email.isnot(None))),
        "forms": await count(select(func.count(AgentForm.id)).where(AgentForm.owner_id == uid, AgentForm.is_enabled.is_(True))),
        "open_houses": await count(select(func.count(OpenHouseEvent.id)).where(OpenHouseEvent.agent_id == uid, OpenHouseEvent.is_deleted.is_(False))),
        "agent_hub_slug": hub.slug if hub else None,
        "has_signature": bool(await count(select(func.count(EmailSignature.id)).where(EmailSignature.owner_id == uid))),
        "idx_site_published": bool(site and site.status == "PUBLISHED"),
        "spotlights_published": await count(select(func.count(PropertyWebsite.id)).where(PropertyWebsite.owner_id == uid, PropertyWebsite.status == "PUBLISHED")),
        "blogs_published": await count(select(func.count(Blog.id)).where(Blog.owner_id == uid, Blog.status == "PUBLISHED")),
        "designs": await count(select(func.count(DesignProject.id)).where(DesignProject.owner_id == uid)),
        "media_assets": await count(select(func.count(StudioMediaAsset.id)).where(StudioMediaAsset.owner_id == uid, StudioMediaAsset.status == "succeeded")),
        "reviews": await count(select(func.count(AgentReview.id)).where(AgentReview.owner_id == uid)),
        "calendar_connected": bool(await count(select(func.count(CalendarConnection.id)).where(CalendarConnection.owner_id == uid))),
        "as_of": now.isoformat(),
    }




@router.get("/plan-terms")
async def get_plan_terms(plan_id: str, current_user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """The real terms of a plan as configured in PayPal (trial length, monthly price, one-time
    setup fee), so checkout's order summary never promises something PayPal won't do."""
    bundle_id = os.getenv("PAYPAL_BUNDLE_PLAN_ID")
    if not (tier_for_plan_id(plan_id) or (bundle_id and plan_id == bundle_id)):
        from app.services.promo_code_service import tier_for_promo_plan
        if not (await tier_for_promo_plan(db, plan_id))[0]:
            raise HTTPException(status_code=404, detail="Unknown plan")
    from app.utils.paypal_plan_terms import get_plan_terms as _terms
    try:
        t = await _terms(plan_id)
    except Exception:
        raise HTTPException(status_code=502, detail="Couldn't reach PayPal")
    return {"trial_days": t["trial_days"], "price": t["price"], "setup_fee": t["setup_fee"], "status": t["status"]}


@router.post("/sync")
async def sync_with_paypal(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Re-read the agent's subscription from PayPal right now - used when they come back from
    approving a plan change, so the new plan shows without waiting for PayPal's webhook."""
    if not current_user.subscription_id:
        return {"success": True, "plan_tier": current_user.plan_tier, "status": current_user.subscription_status}
    from app.utils.subscription_sync import sync_subscription_status
    user = (await db.execute(select(UserModel).where(UserModel.id == current_user.id))).scalar_one()
    ok = await sync_subscription_status(user, db)
    return {"success": ok, "plan_tier": user.plan_tier, "plan_id": user.plan_id, "status": user.subscription_status}


@router.post("/change-plan")
async def change_plan(
    payload: ChangePlanRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Change tier - covers what used to be separate /upgrade and /downgrade endpoints, now
    generalized across all 4 tiers (NO_MLS/BASIC/PREMIUM/IDX). Returns a PayPal approval URL
    the user must visit to consent to the change (or applies immediately if PayPal allows it).

    Always targets the CURRENT price for the destination tier, never a grandfathered legacy
    rate - grandfathering only protects staying on your current tier, not moving to a new one.
    """
    try:
        target_tier = payload.target_tier
        if target_tier not in (t.value for t in PlanTier):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown plan tier: {target_tier}"
            )

        if not tier_allowed_for(current_user, target_tier):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=NOT_IN_BRIGHT_MESSAGE)

        if current_user.plan_tier == target_tier:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"You are already on the {target_tier.replace('_', '-').title()} plan."
            )

        valid_statuses = ["TRIAL", "ACTIVE"]
        if current_user.subscription_status not in valid_statuses:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot change subscription with status: {current_user.subscription_status}"
            )

        if not current_user.subscription_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No subscription found for this user"
            )

        new_plan_id = CURRENT_NO_TRIAL_PLAN_ID_BY_TIER.get(target_tier)
        if not new_plan_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"The {target_tier.replace('_', '-').title()} plan isn't configured yet. Contact support."
            )

        return_url = f"{CLIENT_URL}/settings/subscription?plan_changed=1"
        cancel_url = f"{CLIENT_URL}/settings/subscription"

        # Moving onto IDX for the first time costs the one-time setup fee, unless a code waives it.
        # PayPal never charges a setup fee on a plan change, so the fee path starts a new IDX
        # subscription (fee charged on approval, first month on the date they're already paid up
        # to) and the old one is cancelled when they come back (complete-new).
        if _owes_idx_setup_fee(current_user, target_tier):
            if payload.code:
                new_plan_id = await _apply_idx_waiver(db, current_user.id, payload.code)
            else:
                if not IDX_SETUP_FEE_PLAN_ID:
                    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                        detail="The IDX plan isn't configured yet. Contact support.")
                start = None
                nb = current_user.next_billing_date
                if nb and nb > datetime.now(timezone.utc) + timedelta(minutes=30):
                    start = nb.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                try:
                    created = await paypal_service.create_subscription_with_urls(
                        plan_id=IDX_SETUP_FEE_PLAN_ID,
                        return_url=f"{CLIENT_URL}/settings/subscription?resubscribed=true",
                        cancel_url=cancel_url, start_time=start)
                except Exception:
                    logger.error("PayPal error starting the IDX upgrade", exc_info=True, extra={"user_id": current_user.id})
                    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                        detail="Failed to start the upgrade with PayPal. Please try again.")
                approval = next((l.get("href") for l in created.get("links", []) if l.get("rel") == "approve"), None)
                if not approval:
                    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="PayPal didn't return an approval link. Please try again.")
                return {"success": True, "immediate": False, "approval_url": approval, "setup_fee": True,
                        "message": "Please approve the IDX upgrade on PayPal"}

        # Always use the no-trial plan variant for plan changes to prevent PayPal from resetting
        # trials - our database preserves the original trial_ends_at, so trial users keep their trial.
        try:
            result = await paypal_service.revise_subscription(
                subscription_id=current_user.subscription_id,
                new_plan_id=new_plan_id,
                return_url=return_url,
                cancel_url=cancel_url
            )
        except Exception as e:
            error_str = str(e)
            if "PAYMENT_IN_PROGRESS" in error_str:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A payment is currently being processed for your subscription. PayPal does not allow plan changes while a payment is in progress. Please try again in 30-60 minutes once the transaction is complete."
                )

            logger.error("PayPal API error during plan change", exc_info=True, extra={"user_id": current_user.id, "target_tier": target_tier})
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to initiate the plan change with PayPal. Please try again."
            )

        # Check if change was applied immediately or requires approval
        if result.get("immediate"):
            user_result = await db.execute(
                select(UserModel).where(UserModel.id == current_user.id)
            )
            user = user_result.scalar_one()
            from app.services.website.site_service import reset_idx_site_question_on_upgrade
            reset_idx_site_question_on_upgrade(user, user.plan_tier, target_tier)
            user.plan_tier = target_tier
            user.plan_id = new_plan_id
            user.last_paypal_sync = datetime.now(timezone.utc)
            await db.commit()
            # Moving onto a Showcase plan: build + send every showcase they missed on Basic.
            from app.services.open_house_service import schedule_showcase_backfill
            schedule_showcase_backfill(user.id)

            return {
                "success": True,
                "immediate": True,
                "message": f"Plan changed to {target_tier.replace('_', '-').title()} successfully!"
            }
        else:
            return {
                "success": True,
                "immediate": False,
                "approval_url": result["approval_url"],
                "message": "Please approve the plan change on PayPal"
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process plan change"
        )


@router.post("/cancel")
async def cancel_subscription(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Cancel subscription immediately.
    User keeps access until the end of their billing period (grace period).
    """
    try:
        # Verify subscription exists
        if not current_user.subscription_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No subscription found for this user"
            )

        # Get subscription details BEFORE cancelling to:
        # 1. Verify it's cancellable
        # 2. Extract next_billing_date for grace period
        try:
            subscription_details = await paypal_service.get_subscription(current_user.subscription_id)
            paypal_status = subscription_details.get("status")
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to verify subscription with PayPal. Please try again."
            )

        # Validate subscription status is cancellable
        if paypal_status not in ["ACTIVE", "SUSPENDED"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot cancel subscription with status '{paypal_status}'. Only active or suspended subscriptions can be cancelled."
            )

        # Extract next_billing_date for grace period
        billing_info = subscription_details.get("billing_info", {})
        next_billing_time = billing_info.get("next_billing_time")

        # Call PayPal Cancel API
        try:
            success = await paypal_service.cancel_subscription(
                subscription_id=current_user.subscription_id,
                reason="Customer requested cancellation"
            )
            if not success:
                raise Exception("PayPal returned failure")
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to cancel subscription with PayPal. Please try again."
            )

        # Update database - set status to CANCELLED and save grace period
        result = await db.execute(
            select(UserModel).where(UserModel.id == current_user.id)
        )
        user = result.scalar_one()
        user.subscription_status = "CANCELLED"
        user.last_paypal_sync = datetime.now(timezone.utc)

        # Set grace period (next_billing_date = when their paid period ends)
        if next_billing_time:
            try:
                user.next_billing_date = datetime.fromisoformat(next_billing_time.replace('Z', '+00:00'))
            except Exception as e:
                pass
        else:
            # Fallback: Calculate grace period manually if PayPal doesn't provide it
            # This ensures users always keep access until end of their paid period
            
            # Determine if this was a trial or paid subscription
            trial_days = int(os.getenv("TRIAL_PERIOD_DAYS", "14"))
            # If they had a last_billing_date, they were definitely a paid user
            is_paid_user = user.last_billing_date is not None
            
            grace_days = 30 if is_paid_user else trial_days
            
            if user.last_billing_date:
                # User was billed recently - add grace_days from last billing
                user.next_billing_date = user.last_billing_date + timedelta(days=grace_days)
            elif user.subscription_started_at:
                # Calculate from subscription start date + grace_days
                user.next_billing_date = user.subscription_started_at + timedelta(days=grace_days)
            else:
                # Safety fallback: Give grace_days from now
                user.next_billing_date = datetime.now(timezone.utc) + timedelta(days=grace_days)

        await db.commit()

        return {
            "success": True,
            "message": "Subscription cancelled successfully. You will keep access until the end of your billing period."
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to cancel subscription"
        )


@router.post("/reactivate")
async def reactivate_subscription(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Reactivate a SUSPENDED subscription (payment failed).
    Note: CANCELLED subscriptions should use /create-new endpoint instead.
    """
    try:
        # Verify subscription exists
        if not current_user.subscription_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No subscription found for this user"
            )

        # Verify subscription is SUSPENDED (only status that can be reactivated)
        if current_user.subscription_status != "SUSPENDED":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot reactivate subscription with status: {current_user.subscription_status}. Only SUSPENDED subscriptions can be reactivated. For CANCELLED subscriptions, please create a new subscription."
            )

        # Call PayPal Activate API
        try:
            success = await paypal_service.activate_subscription(
                subscription_id=current_user.subscription_id,
                reason="Customer requested reactivation"
            )
            if not success:
                logger.error(f"PayPal activation returned failure for subscription {current_user.subscription_id}")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="PayPal activation failed. The subscription may be in a state that cannot be reactivated."
                )
        except Exception as e:
            logger.error(f"Exception during PayPal reactivation for user {current_user.id}: {str(e)}", exc_info=True)
            if isinstance(e, HTTPException):
                raise e
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to reactivate subscription with PayPal. Please try again or contact support."
            )

        # Update database - set status to ACTIVE
        result = await db.execute(
            select(UserModel).where(UserModel.id == current_user.id)
        )
        user = result.scalar_one()
        user.subscription_status = "ACTIVE"
        await db.commit()

        return {
            "success": True,
            "message": "Subscription reactivated successfully!"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reactivate subscription"
        )


class CreateSubscriptionRequest(BaseModel):
    plan_tier: str
    code: Optional[str] = None  # waives the one-time IDX setup fee


class CompleteSubscriptionRequest(BaseModel):
    subscription_id: str


@router.post("/complete-new")
async def complete_new_subscription(
    request: CompleteSubscriptionRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Complete a new subscription after user approves it on PayPal.
    This updates the user's database record with the new subscription details.

    Called automatically by frontend when redirected back from PayPal.

    Args:
        subscription_id: The PayPal subscription ID from the return URL

    Returns:
        success status and updated subscription info
    """
    try:
        subscription_id = request.subscription_id

        # Step 1: Check if subscription is already claimed by another user (prevents hijacking)
        existing_user_result = await db.execute(
            select(UserModel).where(UserModel.subscription_id == subscription_id)
        )
        existing_user = existing_user_result.scalar_one_or_none()

        if existing_user and existing_user.id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This subscription is already claimed by another account"
            )

        # Their own current subscription (a page refresh, or PayPal's return from approving a
        # plan change) - just re-read it from PayPal, never restart it as a brand-new one.
        if current_user.subscription_id == subscription_id:
            from app.utils.subscription_sync import sync_subscription_status
            fresh = (await db.execute(select(UserModel).where(UserModel.id == current_user.id))).scalar_one()
            await sync_subscription_status(fresh, db)
            return {
                "success": True,
                "message": f"You're on the {display_name_for_tier(fresh.plan_tier)} plan.",
                "subscription": {"subscription_id": subscription_id, "plan_tier": fresh.plan_tier,
                                 "status": fresh.subscription_status,
                                 "trial_ends_at": fresh.trial_ends_at.isoformat() if fresh.trial_ends_at else None},
            }

        # A brand-new subscription is only for someone whose old one has ended or failed -
        # otherwise they'd be paying for two at once. The one exception is the IDX upgrade with its
        # setup fee: that new subscription replaces the live one (cancelled below).
        live = current_user.subscription_status not in ("CANCELLED", "EXPIRED", "SUSPENDED", "PENDING_PAYMENT", None)
        if live and not (current_user.plan_tier != "IDX" and IDX_SETUP_FEE_PLAN_ID):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You already have an active subscription. Change plans from Settings > Billing instead."
            )

        # Step 2: Validate subscription with PayPal API
        try:
            subscription_details = await paypal_service.get_subscription(subscription_id)
            # logger.info(f"PAYPAL DEBUG: Full Subscription Details: {subscription_details}") # Added for debugging
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid subscription ID or PayPal service unavailable"
            )

        # Step 3: Get plan details from PayPal
        paypal_plan_id = subscription_details.get('plan_id')
        subscription_status = subscription_details.get('status')

        # Extract billing information for new subscription
        billing_info = subscription_details.get('billing_info', {})
        next_billing_time = billing_info.get('next_billing_time')

        # Step 3: Verify subscription status is valid - approved on PayPal, not just started
        if subscription_status not in ['ACTIVE', 'APPROVED']:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="PayPal hasn't confirmed this subscription yet. Please finish approving it on PayPal."
            )

        if live and paypal_plan_id != IDX_SETUP_FEE_PLAN_ID:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You already have an active subscription. Change plans from Settings > Billing instead."
            )

        # Step 4: Determine plan tier from plan_id (legacy or current, any of the 4 tiers, or a code's plan)
        plan_tier = tier_for_plan_id(paypal_plan_id)
        if plan_tier is None:
            from app.services.promo_code_service import tier_for_promo_plan
            plan_tier = (await tier_for_promo_plan(db, paypal_plan_id))[0]
        if plan_tier is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid plan ID from PayPal"
            )
        if not tier_allowed_for(current_user, plan_tier):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=NOT_IN_BRIGHT_MESSAGE)

        # Step 5: Update user's subscription in database
        result = await db.execute(
            select(UserModel).where(UserModel.id == current_user.id)
        )
        user = result.scalar_one()

        # Determine if this plan has a trial period
        has_trial = plan_id_has_trial(paypal_plan_id)
        trial_days = int(os.getenv("TRIAL_PERIOD_DAYS", "30"))

        # The old subscription they're replacing (e.g. a suspended one after a failed card)
        # must stop, or PayPal could retry/charge it alongside the new one.
        old_subscription_id = user.subscription_id
        if old_subscription_id and old_subscription_id != subscription_id:
            try:
                old_details = await paypal_service.get_subscription(old_subscription_id)
                if old_details.get("status") in ("ACTIVE", "SUSPENDED", "APPROVED"):
                    await paypal_service.cancel_subscription(old_subscription_id, reason="Replaced by a new subscription")
            except Exception:
                logger.error("Could not cancel the replaced PayPal subscription - cancel it by hand",
                             exc_info=True, extra={"user_id": user.id, "old_subscription_id": old_subscription_id})
                try:
                    from app.services.discord_notifier import notifier
                    notifier.send(f":warning: Couldn't cancel old PayPal subscription {old_subscription_id} for {user.email} after they resubscribed. Cancel it in PayPal so they aren't billed twice.")
                except Exception:
                    pass

        # Update subscription fields
        now = datetime.now(timezone.utc)
        user.subscription_id = subscription_id
        user.plan_id = paypal_plan_id
        from app.services.website.site_service import reset_idx_site_question_on_upgrade
        reset_idx_site_question_on_upgrade(user, user.plan_tier, plan_tier)
        user.plan_tier = plan_tier
        user.subscription_started_at = now
        user.last_paypal_sync = now
        if plan_tier == "IDX" and not user.idx_setup_settled_at:
            user.idx_setup_settled_at = now

        if has_trial:
            # New customer with trial - set trial end date from PayPal data
            user.subscription_status = "TRIAL"

            if next_billing_time:
                try:
                    trial_end = datetime.fromisoformat(next_billing_time.replace('Z', '+00:00'))
                except Exception:
                    trial_end = now + timedelta(days=trial_days)
            else:
                trial_end = now + timedelta(days=trial_days)
                
            user.trial_ends_at = trial_end
            trial_end_iso = trial_end.isoformat()
            user.next_billing_date = trial_end
        else:
            # Returning customer without trial
            user.subscription_status = "ACTIVE"
            user.trial_ends_at = None
            trial_end_iso = None

        # Clear old subscription billing data and set new subscription billing info
        user.last_billing_date = None  # No payment made yet for new subscription

        # Set next billing date for new subscription
        if next_billing_time:
            # PayPal provided next billing time - use it
            try:
                user.next_billing_date = datetime.fromisoformat(next_billing_time.replace('Z', '+00:00'))
            except Exception as e:
                # Fallback to calculation
                if has_trial:
                    user.next_billing_date = trial_end  # First billing when trial ends
                else:
                    user.next_billing_date = now + timedelta(days=30)  # Monthly billing cycle
        else:
            # PayPal didn't provide next billing time - calculate it
            if has_trial:
                user.next_billing_date = trial_end  # First billing when trial ends
            else:
                user.next_billing_date = now + timedelta(days=30)  # Monthly billing cycle

        await db.commit()
        # A returning Basic agent resubscribing on a Showcase plan gets their backlog sent too.
        from app.services.open_house_service import schedule_showcase_backfill
        schedule_showcase_backfill(user.id)

        # Return appropriate message
        trial_message = f"with {trial_days}-day trial!" if has_trial else "(no trial - billing starts immediately)"

        return {
            "success": True,
            "message": f"Successfully subscribed to {display_name_for_tier(plan_tier)} plan {trial_message}",
            "subscription": {
                "subscription_id": subscription_id,
                "plan_tier": plan_tier,
                "status": "TRIAL" if has_trial else "ACTIVE",
                "trial_ends_at": trial_end_iso
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to complete new subscription"
        )


@router.post("/create-new")
async def create_new_subscription(
    request: CreateSubscriptionRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new subscription for users with cancelled or expired subscriptions.
    Allows choosing between Basic and Premium plans.

    Args:
        plan_tier: "BASIC" or "PREMIUM"

    Returns:
        approval_url: PayPal URL where user must approve the new subscription
    """
    try:
        plan_tier = request.plan_tier

        # Validate plan_tier - all 4 tiers, not just Basic/Premium
        if plan_tier not in (t.value for t in PlanTier):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid plan_tier: {plan_tier}"
            )

        if not tier_allowed_for(current_user, plan_tier):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=NOT_IN_BRIGHT_MESSAGE)

        # Verify user has a cancelled, expired, or suspended subscription
        valid_statuses = ["CANCELLED", "EXPIRED", "SUSPENDED"]
        if current_user.subscription_status not in valid_statuses:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot create new subscription with status: {current_user.subscription_status}. This endpoint is for CANCELLED, EXPIRED, or SUSPENDED subscriptions."
            )

        # Get the appropriate no-trial plan ID (returning customers don't get trials). Always
        # the CURRENT price, never the legacy Basic/Premium IDs - a full resubscribe after
        # cancellation is not covered by grandfathering (that only protects staying on your
        # existing tier without ever lapsing).
        plan_id = CURRENT_NO_TRIAL_PLAN_ID_BY_TIER.get(plan_tier)
        # Coming back on IDX without ever having paid the setup fee: it applies, unless a code waives it.
        if _owes_idx_setup_fee(current_user, plan_tier):
            plan_id = (await _apply_idx_waiver(db, current_user.id, request.code)) if request.code else IDX_SETUP_FEE_PLAN_ID

        if not plan_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="No-trial plan ID not configured. Please contact support."
            )

        # Build return URLs
        return_url = f"{CLIENT_URL}/settings/subscription?resubscribed=true"
        cancel_url = f"{CLIENT_URL}/settings/subscription"

        # Create new subscription via PayPal
        try:
            subscription_data = await paypal_service.create_subscription_with_urls(
                plan_id=plan_id,
                return_url=return_url,
                cancel_url=cancel_url
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create subscription with PayPal. Please try again."
            )

        # Extract approval URL from response
        approval_url = None
        for link in subscription_data.get("links", []):
            if link.get("rel") == "approve":
                approval_url = link.get("href")
                break

        if not approval_url:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to get PayPal approval URL. Please try again."
            )

        # Note: We don't update the database yet - the webhook will handle that
        # when the user completes the subscription on PayPal

        return {
            "success": True,
            "approval_url": approval_url,
            "plan_tier": plan_tier,
            "message": f"Please approve your new {display_name_for_tier(plan_tier)} subscription on PayPal"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create new subscription"
        )
