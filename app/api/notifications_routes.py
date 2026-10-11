from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func
from typing import List, Optional
from datetime import datetime, timedelta, timezone

from app.database import get_db
from app.models.database import Notification, User
from app.utils.auth import get_current_active_user
from app.schemas.notification import (
    NotificationResponse,
    NotificationUnreadCountResponse,
    NotificationPreferencesResponse,
    NotificationPreferencesUpdate,
)
from app.services import notification_preferences_service
from app.config.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()


@router.get("/api/v1/notifications", response_model=List[NotificationResponse])
async def get_notifications(
    unread_only: bool = Query(False, description="Only return unread notifications"),
    max_age_days: Optional[int] = Query(None, ge=1, description="Only return notifications from the last N days - used by the bell dropdown so it doesn't grow forever; omit for the full-history Activity page"),
    offset: int = Query(0, ge=0, description="Activity page 'Load older': skip this many of the newest"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get notifications for the current agent"""
    try:
        # Build query
        query = select(Notification).where(Notification.agent_id == current_user.id)

        if unread_only:
            query = query.where(Notification.is_read == False)

        if max_age_days:
            cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
            query = query.where(Notification.created_at >= cutoff)

        # Order by created_at descending (most recent first). The bell is now a history mailbox
        # (read notifications stay visible instead of disappearing), so cap the page size - without
        # unread_only filtering it out, this list only grows over an agent's lifetime. The full
        # Activity page (no max_age_days) gets a taller cap since it's the one place meant to show
        # everything, not just what's recent enough for the bell.
        query = query.order_by(Notification.created_at.desc(), Notification.id).offset(offset).limit(100 if max_age_days else 300)

        result = await db.execute(query)
        notifications = result.scalars().all()

        return notifications

    except Exception as e:
        logger.error("fetching notifications failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to fetch notifications")


@router.get("/api/v1/notifications/unread-count", response_model=NotificationUnreadCountResponse)
async def get_unread_count(
    reference_type: Optional[List[str]] = Query(None, description="Only count unread notifications for these reference_type(s), e.g. HOST_REQUEST"),
    type: Optional[List[str]] = Query(None, description="Only count unread notifications for these type(s), e.g. OPEN_HOUSE_SIGN_IN - use instead of reference_type when reference_type values collide across features"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get count of unread notifications for the current agent"""
    try:
        conditions = [
            Notification.agent_id == current_user.id,
            Notification.is_read == False,
        ]
        if reference_type:
            conditions.append(Notification.reference_type.in_(reference_type))
        if type:
            conditions.append(Notification.type.in_(type))
        query = select(func.count(Notification.id)).where(and_(*conditions))

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
        notification.read_at = datetime.now(timezone.utc)

        await db.commit()
        await db.refresh(notification)

        return notification

    except HTTPException:
        raise
    except Exception as e:
        logger.error("marking notification as read failed", extra={"error": str(e)})
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to mark notification as read")


@router.post("/api/v1/notifications/mark-types-as-read")
async def mark_types_as_read(
    types: List[str] = Query(..., description="Notification 'type' values to mark as read, e.g. HOST_REQUEST_APPROVED"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Mark all unread notifications of the given type(s) as read for the current agent.
    Used to clear the 'unseen update' badges the moment their panel is opened."""
    try:
        query = select(Notification).where(
            and_(
                Notification.agent_id == current_user.id,
                Notification.is_read == False,
                Notification.type.in_(types),
            )
        )
        result = await db.execute(query)
        notifications = result.scalars().all()

        read_time = datetime.now(timezone.utc)
        for notification in notifications:
            notification.is_read = True
            notification.read_at = read_time

        await db.commit()

        return {"success": True, "message": f"Marked {len(notifications)} notifications as read"}

    except Exception as e:
        logger.error("marking notifications by type as read failed", extra={"error": str(e)})
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to mark notifications as read")


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
        read_time = datetime.now(timezone.utc)
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


@router.get("/api/v1/notifications/preferences", response_model=NotificationPreferencesResponse)
async def get_notification_preferences(
    current_user: User = Depends(get_current_active_user),
):
    """The resolved email/push opt-out matrix for the current agent, plus the row metadata the
    Settings screen renders. The in-app bell is always on and is not represented here."""
    return NotificationPreferencesResponse(
        categories=notification_preferences_service.categories_payload(),
        preferences=notification_preferences_service.resolve_prefs(current_user.notification_prefs),
        push_available=False,
    )


@router.put("/api/v1/notifications/preferences", response_model=NotificationPreferencesResponse)
async def update_notification_preferences(
    payload: NotificationPreferencesUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Replace the agent's email/push preference matrix. Unknown categories/channels are dropped;
    anything omitted falls back to its default."""
    try:
        current_user.notification_prefs = notification_preferences_service.sanitize_incoming(payload.preferences)
        await db.commit()
        return NotificationPreferencesResponse(
            categories=notification_preferences_service.categories_payload(),
            preferences=notification_preferences_service.resolve_prefs(current_user.notification_prefs),
            push_available=False,
        )
    except Exception as e:
        logger.error("saving notification preferences failed", extra={"error": str(e)})
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to save notification preferences")
