from fastapi import APIRouter, HTTPException, Depends, status, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timezone
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
    Reactivate a cancelled or suspended subscription.
    """
    try:
        # Verify subscription exists
        if not current_user.subscription_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No subscription found for this user"
            )

        # Verify subscription is cancelled or suspended
        if current_user.subscription_status not in ["CANCELLED", "SUSPENDED"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot reactivate subscription with status: {current_user.subscription_status}"
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
