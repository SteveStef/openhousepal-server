from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, func
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
from app.services.notification_links import exact_links, showcase_links
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
            # Recent ones, plus anything still unread however old - so the bell, Dash and the
            # Activity page always count the same unread items.
            cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
            query = query.where(or_(Notification.created_at >= cutoff, Notification.is_read == False))

        # Order by created_at descending (most recent first). The bell is now a history mailbox
        # (read notifications stay visible instead of disappearing), so cap the page size - without
        # unread_only filtering it out, this list only grows over an agent's lifetime. The full
        # Activity page (no max_age_days) gets a taller cap since it's the one place meant to show
        # everything, not just what's recent enough for the bell.
        query = query.order_by(Notification.created_at.desc(), Notification.id).offset(offset).limit(100 if max_age_days else 300)

        result = await db.execute(query)
        notifications = result.scalars().all()
        if max_age_days and not unread_only and not offset:
            # Every unread one is in the bell / Dash list even past the newest 100 - so their unread
            # count always matches the Activity page.
            have = {n.id for n in notifications}
            more = (await db.execute(
                select(Notification).where(Notification.agent_id == current_user.id, Notification.is_read == False)
                .order_by(Notification.created_at.desc()).limit(100)
            )).scalars().all()
            extra = [n for n in more if n.id not in have]
            if extra:
                notifications = sorted([*notifications, *extra], key=lambda n: n.created_at, reverse=True)

        # Each one opens the exact thing it talks about (see notification_links) - computed here,
        # never written back, so the stored link stays the fallback.
        links = await exact_links(db, current_user.id, notifications)
        # A form answer that built a Showcase also gets a button straight to it.
        showcases = await showcase_links(db, current_user.id, notifications)
        return [
            NotificationResponse.model_validate(n).model_copy(update={
                **({"link": links[n.id]} if n.id in links else {}),
                **({"showcase_link": showcases[n.id]} if n.id in showcases else {}),
            }) if n.id in links or n.id in showcases else n
            for n in notifications
        ]

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


# ---------------------------------------------------------------- Studio -> Email (one agent's own settings)

async def _email_settings_payload(db: AsyncSession, user: User) -> dict:
    from app.models.database import MailConnection
    from app.services import email_settings_service as es
    conn = (await db.execute(select(MailConnection).where(MailConnection.owner_id == user.id))).scalar_one_or_none()
    connected = bool(conn and conn.enabled and conn.account_email and conn.status == "ok")
    s = es.resolve(user.email_settings)
    prefs = notification_preferences_service.resolve_prefs(user.notification_prefs)
    cats = {c["id"]: c for c in notification_preferences_service.categories_payload()}
    # Only what this plan actually sends. No-MLS plans have no Showcases, Spotlights or hosting
    # (Bright MLS features) - their clients only get tour confirmations from this list.
    from app.utils.auth import REVIEWS_ACCESS_TIERS, STUDIO_MEDIA_ACCESS_TIERS, tier_has_mls
    plan = getattr(user, "plan_tier", None)
    client_rows = es.CLIENT_EMAILS
    hidden_cats: set = set()
    if plan and not tier_has_mls(plan):
        client_rows = [c for c in es.CLIENT_EMAILS if c["key"] == "tour_confirmed"]
        hidden_cats = {"showcase_requests", "host_requests", "showcase_digest", "liked_homes", "showcase_activity", "spotlight_weekly", "listing_updates", "team"}
        if plan not in REVIEWS_ACCESS_TIERS:
            hidden_cats.add("reviews")
        if plan not in STUDIO_MEDIA_ACCESS_TIERS:
            hidden_cats.add("ai_media")
    receive = [{**t, "categories": [{**cats[c], "email": prefs[c]["email"]} for c in t["categories"] if c in cats and c not in hidden_cats]} for t in es.RECEIVE_TIERS]
    return {
        "client": [{k: v for k, v in {**c, **s["client"][c["key"]]}.items() if k != "templates"} for c in client_rows],
        "invite_delay": s["invite_delay"], "invite_delays": list(es.INVITE_DELAYS),
        "briefing_time": s["briefing_time"], "briefing_times": list(es.BRIEFING_TIMES),
        "mailbox": {"connected": connected, "account_email": conn.account_email if conn else None,
                    "provider": conn.provider if conn else None, "status": conn.status if conn else None},
        "receive": [t for t in receive if t["categories"]],
        "always_on": es.ALWAYS_ON,
    }


@router.get("/api/v1/email-settings")
async def get_email_settings(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    """Everything on Studio -> Email for the signed-in agent only."""
    return await _email_settings_payload(db, current_user)


@router.put("/api/v1/email-settings")
async def update_email_settings(payload: dict, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)):
    """Partial update: {client: {key: {on, via, mode}}, invite_delay, briefing_time, receive: {category: bool}}.
    Saved on this agent's own account; nothing here can add emails beyond the defaults."""
    from app.services import email_settings_service as es
    try:
        user = await db.get(User, current_user.id)
        user.email_settings = es.sanitize(payload, user.email_settings)
        receive = payload.get("receive") if isinstance(payload, dict) else None
        if isinstance(receive, dict) and receive:
            current = notification_preferences_service.resolve_prefs(user.notification_prefs)
            for cat_id, on in receive.items():
                if cat_id in current and isinstance(on, bool):
                    current[cat_id]["email"] = on
            user.notification_prefs = notification_preferences_service.sanitize_incoming(current)
        await db.commit()
        await db.refresh(user)
        return await _email_settings_payload(db, user)
    except Exception as e:
        logger.error("saving email settings failed", extra={"error": str(e)})
        await db.rollback()
        raise HTTPException(status_code=500, detail="Couldn't save your email settings - please try again.")
