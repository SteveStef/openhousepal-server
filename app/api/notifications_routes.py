from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func
from typing import List, Optional
from datetime import datetime

from app.database import get_db
from app.models.database import Notification, User
from app.utils.auth import get_current_active_user
from app.schemas.notification import NotificationResponse, NotificationUnreadCountResponse
from app.config.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()


@router.get("/api/v1/notifications", response_model=List[NotificationResponse])
async def get_notifications(
    unread_only: bool = Query(False, description="Only return unread notifications"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get notifications for the current agent"""
    try:
        # Build query
        query = select(Notification).where(Notification.agent_id == current_user.id)

        if unread_only:
            query = query.where(Notification.is_read == False)

        # Order by created_at descending (most recent first)
        query = query.order_by(Notification.created_at.desc())

        result = await db.execute(query)
        notifications = result.scalars().all()

        return notifications

    except Exception as e:
        logger.error("fetching notifications failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to fetch notifications")


@router.get("/api/v1/notifications/unread-count", response_model=NotificationUnreadCountResponse)
async def get_unread_count(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get count of unread notifications for the current agent"""
    try:
        query = select(func.count(Notification.id)).where(
            and_(
                Notification.agent_id == current_user.id,
                Notification.is_read == False
            )
        )

        result = await db.execute(query)
        count = result.scalar()

        return NotificationUnreadCountResponse(unread_count=count or 0)

    except Exception as e:
        logger.error("fetching unread count failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to fetch unread count")


@router.patch("/api/v1/notifications/{notification_id}/mark-as-read", response_model=NotificationResponse)
async def mark_as_read(
    notification_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Mark a notification as read"""
    try:
        # Fetch the notification
        query = select(Notification).where(
            and_(
                Notification.id == notification_id,
                Notification.agent_id == current_user.id
            )
        )
        result = await db.execute(query)
        notification = result.scalar_one_or_none()

        if not notification:
            raise HTTPException(status_code=404, detail="Notification not found")

        # Mark as read
        notification.is_read = True
        notification.read_at = datetime.utcnow()

        await db.commit()
        await db.refresh(notification)

        return notification

    except HTTPException:
        raise
    except Exception as e:
        logger.error("marking notification as read failed", extra={"error": str(e)})
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to mark notification as read")


@router.post("/api/v1/notifications/mark-all-as-read")
async def mark_all_as_read(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Mark all notifications as read for the current agent"""
    try:
        # Fetch all unread notifications for the agent
        query = select(Notification).where(
            and_(
                Notification.agent_id == current_user.id,
                Notification.is_read == False
            )
        )
        result = await db.execute(query)
        notifications = result.scalars().all()

        # Mark all as read
        read_time = datetime.utcnow()
        for notification in notifications:
            notification.is_read = True
            notification.read_at = read_time

        await db.commit()

        return {
            "success": True,
            "message": f"Marked {len(notifications)} notifications as read"
        }

    except Exception as e:
        logger.error("marking all notifications as read failed", extra={"error": str(e)})
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to mark all notifications as read")
