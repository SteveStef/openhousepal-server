from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from typing import List, Dict, Any

from app.database import get_db
from app.models.database import User as UserModel
from app.schemas.user import User as UserSchema
from app.utils.auth import require_admin_user
from app.config.logging import get_logger

router = APIRouter(prefix="/admin", tags=["admin"])
logger = get_logger(__name__)

@router.get("/users", response_model=List[UserSchema])
async def list_users(
    db: AsyncSession = Depends(get_db),
    admin: UserModel = Depends(require_admin_user)
):
    """
    List all registered users (Admin only).
    """
    try:
        stmt = select(UserModel).order_by(UserModel.created_at.desc())
        result = await db.execute(stmt)
        return result.scalars().all()
    except Exception as e:
        logger.error(f"Failed to list users: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve users list"
        )

@router.patch("/users/{user_id}/authorize")
async def toggle_broker_authorization(
    user_id: str,
    authorized: bool,
    db: AsyncSession = Depends(get_db),
    admin: UserModel = Depends(require_admin_user)
):
    """
    Grant or revoke broker authorization for a specific user (Admin only).
    """
    try:
        stmt = select(UserModel).where(UserModel.id == user_id)
        result = await db.execute(stmt)
        user = result.scalar_one_or_none()
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
            
        user.broker_authorized = authorized
        await db.commit()
        await db.refresh(user)
        
        return {
            "success": True,
            "user_id": user_id,
            "broker_authorized": user.broker_authorized
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to authorize user {user_id}: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update authorization status"
        )

@router.patch("/users/{user_id}/plan")
async def update_user_plan(
    user_id: str,
    plan_tier: str,
    db: AsyncSession = Depends(get_db),
    admin: UserModel = Depends(require_admin_user)
):
    """
    Update a user's plan tier and status (Admin only).
    Setting a plan manually clears the subscription_id, making it a "lifetime" 
    or "admin-injected" plan that doesn't expire via PayPal sync.
    """
    try:
        stmt = select(UserModel).where(UserModel.id == user_id)
        result = await db.execute(stmt)
        user = result.scalar_one_or_none()
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
            
        # reset the user to nothing
        if plan_tier == "FREE":
            user.plan_tier = None
            user.subscription_status = "PENDING_PAYMENT"
            user.subscription_id = None
            user.plan_id = None
            user.trial_ends_at = None
            user.next_billing_date = None
        else:
            # For BASIC or PREMIUM, set to ACTIVE and clear subscription_id 
            # and plan_id to make it "injected" (it won't be synced from PayPal)
            user.plan_tier = plan_tier
            user.subscription_status = "ACTIVE"
            user.subscription_id = None
            user.plan_id = None
            user.trial_ends_at = None
            user.next_billing_date = None
            
        await db.commit()
        await db.refresh(user)
        
        return {
            "success": True,
            "user_id": user_id,
            "plan_tier": user.plan_tier,
            "subscription_status": user.subscription_status
        }
    except Exception as e:
        logger.error(f"Failed to update plan for user {user_id}: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update user plan"
        )
