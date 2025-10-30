from fastapi import APIRouter, HTTPException, Depends, status, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timezone, timedelta
from pydantic import BaseModel
import os

from app.database import get_db
from app.schemas.user import User
from app.services.paypal_service import paypal_service
from app.utils.auth import get_current_active_user
from app.models.database import User as UserModel

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])

# PayPal Plan ID Constants (from environment variables)
BASIC_PLAN_ID = os.getenv("PAYPAL_BASIC_PLAN_ID")
PREMIUM_PLAN_ID = os.getenv("PAYPAL_PREMIUM_PLAN_ID")

BASIC_PLAN_NO_TRIAL_ID = os.getenv("PAYPAL_BASIC_NO_TRIAL_PLAN_ID")
PREMIUM_PLAN_NO_TRIAL_ID = os.getenv("PAYPAL_PREMIUM_NO_TRIAL_PLAN_ID")

CLIENT_URL = os.getenv("CLIENT_URL", "http://localhost:3000")


@router.post("/upgrade")
async def upgrade_subscription(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Upgrade from Basic to Premium plan.
    Returns PayPal approval URL that user must visit to consent to the upgrade.
    """
    try:
        # Verify user is on BASIC plan
        if current_user.plan_tier != "BASIC":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You are not on the Basic plan. Cannot upgrade."
            )

        # Verify subscription status is valid for upgrade
        valid_statuses = ["TRIAL", "ACTIVE"]
        if current_user.subscription_status not in valid_statuses:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot upgrade subscription with status: {current_user.subscription_status}"
            )

        # Verify subscription_id exists
        if not current_user.subscription_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No subscription found for this user"
            )

        # Build return URLs - just redirect back to subscription page
        # Webhook will handle updating the database with new plan
        return_url = f"{CLIENT_URL}/settings/subscription"
        cancel_url = f"{CLIENT_URL}/settings/subscription"

        # Call PayPal Revise API
        try:
            result = await paypal_service.revise_subscription(
                subscription_id=current_user.subscription_id,
                new_plan_id=PREMIUM_PLAN_ID,
                return_url=return_url,
                cancel_url=cancel_url
            )
        except Exception as e:
            print(f"PayPal API error during upgrade: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to initiate upgrade with PayPal. Please try again."
            )

        # Check if change was applied immediately or requires approval
        if result.get("immediate"):
            # Plan change applied immediately - update database
            user_result = await db.execute(
                select(UserModel).where(UserModel.id == current_user.id)
            )
            user = user_result.scalar_one()
            user.plan_tier = "PREMIUM"
            user.plan_id = PREMIUM_PLAN_ID
            user.last_paypal_sync = datetime.now(timezone.utc)
            await db.commit()

            return {
                "success": True,
                "immediate": True,
                "message": "Plan upgraded to Premium successfully!"
            }
        else:
            # Requires user approval
            return {
                "success": True,
                "immediate": False,
                "approval_url": result["approval_url"],
                "message": "Please approve the plan change on PayPal"
            }

    except HTTPException:
        raise
    except Exception as e:
        print(f"Upgrade subscription error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process upgrade"
        )


@router.post("/downgrade")
async def downgrade_subscription(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Downgrade from Premium to Basic plan.
    Returns PayPal approval URL that user must visit to consent to the downgrade.
    """
    try:
        # Verify user is on PREMIUM plan
        if current_user.plan_tier != "PREMIUM":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You are not on the Premium plan. Cannot downgrade."
            )

        # Verify subscription status is valid for downgrade
        valid_statuses = ["TRIAL", "ACTIVE"]
        if current_user.subscription_status not in valid_statuses:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot downgrade subscription with status: {current_user.subscription_status}"
            )

        # Verify subscription_id exists
        if not current_user.subscription_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No subscription found for this user"
            )

        # Build return URLs - just redirect back to subscription page
        # Webhook will handle updating the database with new plan
        return_url = f"{CLIENT_URL}/settings/subscription"
        cancel_url = f"{CLIENT_URL}/settings/subscription"

        # Call PayPal Revise API
        try:
            result = await paypal_service.revise_subscription(
                subscription_id=current_user.subscription_id,
                new_plan_id=BASIC_PLAN_ID,
                return_url=return_url,
                cancel_url=cancel_url
            )
        except Exception as e:
            print(f"PayPal API error during downgrade: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to initiate downgrade with PayPal. Please try again."
            )

        # Check if change was applied immediately or requires approval
        if result.get("immediate"):
            # Plan change applied immediately - update database
            user_result = await db.execute(
                select(UserModel).where(UserModel.id == current_user.id)
            )
            user = user_result.scalar_one()
            user.plan_tier = "BASIC"
            user.plan_id = BASIC_PLAN_ID
            user.last_paypal_sync = datetime.now(timezone.utc)
            await db.commit()

            return {
                "success": True,
                "immediate": True,
                "message": "Plan downgraded to Basic successfully!"
            }
        else:
            # Requires user approval
            return {
                "success": True,
                "immediate": False,
                "approval_url": result["approval_url"],
                "message": "Please approve the plan change on PayPal"
            }

    except HTTPException:
        raise
    except Exception as e:
        print(f"Downgrade subscription error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process downgrade"
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
            print(f"PayPal API error fetching subscription: {e}")
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
            print(f"PayPal API error during cancellation: {e}")
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
                print(f"Grace period set until: {next_billing_time}")
            except Exception as e:
                print(f"Warning: Failed to parse next_billing_time: {e}")

        await db.commit()

        return {
            "success": True,
            "message": "Subscription cancelled successfully. You will keep access until the end of your billing period."
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"Cancel subscription error: {e}")
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
                raise Exception("PayPal returned failure")
        except Exception as e:
            print(f"PayPal API error during reactivation: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to reactivate subscription with PayPal. Please try again."
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
        print(f"Reactivate subscription error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reactivate subscription"
        )


class CreateSubscriptionRequest(BaseModel):
    plan_tier: str


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

        # Step 1: Validate subscription with PayPal API
        try:
            subscription_details = await paypal_service.get_subscription(subscription_id)
        except Exception as e:
            print(f"PayPal API error: {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid subscription ID or PayPal service unavailable"
            )

        # Step 2: Get plan details from PayPal
        paypal_plan_id = subscription_details.get('plan_id')
        subscription_status = subscription_details.get('status')

        print(f"📝 Completing subscription {subscription_id} for user {current_user.email}")
        print(f"   Plan ID: {paypal_plan_id}, Status: {subscription_status}")

        # Step 3: Verify subscription status is valid
        if subscription_status not in ['ACTIVE', 'APPROVAL_PENDING', 'APPROVED']:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid subscription status: {subscription_status}"
            )

        # Step 4: Determine plan tier from plan_id
        if paypal_plan_id == BASIC_PLAN_ID or paypal_plan_id == BASIC_PLAN_NO_TRIAL_ID:
            plan_tier = "BASIC"
        elif paypal_plan_id == PREMIUM_PLAN_ID or paypal_plan_id == PREMIUM_PLAN_NO_TRIAL_ID:
            plan_tier = "PREMIUM"
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid plan ID from PayPal"
            )

        # Step 5: Update user's subscription in database
        result = await db.execute(
            select(UserModel).where(UserModel.id == current_user.id)
        )
        user = result.scalar_one()

        # Determine if this plan has a trial period
        has_trial = paypal_plan_id in [BASIC_PLAN_ID, PREMIUM_PLAN_ID]

        # Update subscription fields
        now = datetime.now(timezone.utc)
        user.subscription_id = subscription_id
        user.plan_id = paypal_plan_id
        user.plan_tier = plan_tier
        user.subscription_started_at = now
        user.last_paypal_sync = now

        if has_trial:
            # New customer with trial
            trial_end = now + timedelta(days=14)
            user.subscription_status = "TRIAL"
            user.trial_ends_at = trial_end
            trial_end_iso = trial_end.isoformat()
            print(f"✅ Subscription completed: {user.email} -> {plan_tier} (TRIAL until {trial_end})")
        else:
            # Returning customer without trial
            user.subscription_status = "ACTIVE"
            user.trial_ends_at = None
            trial_end_iso = None
            print(f"✅ Subscription completed: {user.email} -> {plan_tier} (ACTIVE - no trial)")

        await db.commit()

        # Return appropriate message
        trial_message = "with 14-day trial!" if has_trial else "(no trial - billing starts immediately)"

        return {
            "success": True,
            "message": f"Successfully subscribed to {plan_tier.title()} plan {trial_message}",
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
        print(f"Complete new subscription error: {e}")
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

        # Validate plan_tier
        if plan_tier not in ["BASIC", "PREMIUM"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid plan_tier. Must be 'BASIC' or 'PREMIUM'"
            )

        # Verify user has a cancelled or expired subscription
        valid_statuses = ["CANCELLED", "EXPIRED"]
        if current_user.subscription_status not in valid_statuses:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot create new subscription with status: {current_user.subscription_status}. This endpoint is only for CANCELLED or EXPIRED subscriptions."
            )

        # Get the appropriate no-trial plan ID (returning customers don't get trials)
        plan_id = PREMIUM_PLAN_NO_TRIAL_ID if plan_tier == "PREMIUM" else BASIC_PLAN_NO_TRIAL_ID

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
            print(f"PayPal API error during subscription creation: {e}")
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
            "message": f"Please approve your new {plan_tier.title()} subscription on PayPal"
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"Create new subscription error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create new subscription"
        )
