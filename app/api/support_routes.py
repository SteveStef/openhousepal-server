"""Agent-facing support ticket endpoints - an agent's own tickets and conversation threads
with support. The admin-side counterpart (list every agent's tickets, reply, close/reopen)
lives in admin_support_routes.py behind require_admin_user.

Unread tracking is timestamp-based rather than a stored boolean: a ticket reads as unread for
a viewer when the ticket's updated_at is newer than that viewer's own *_last_read_at AND the
newest message wasn't sent by that same viewer (so replying to your own ticket never marks it
unread for you). Viewing a ticket's detail stamps the viewer's own *_last_read_at = now().

Every ticket carries a category the agent picks before writing (Contact support / Report a bug /
Suggest a feature), plus a small whitelisted `details` blob the browser fills in quietly (the
page they came from, browser/device/screen, urgency) and optional screenshots on the first
message - all shown to the admin beside the thread.
"""
from datetime import datetime, timezone
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.database import SupportTicket, SupportTicketMessage, User as UserModel
from app.schemas.support import (
    TicketCreate, MessageCreate, TicketDetail, TicketListItem, TicketUnreadCountResponse,
    TicketStatusUpdate, ScreenshotUploadResponse,
)
from app.services import s3_service
from app.utils.image_validation import photo_to_jpeg
from app.utils.auth import get_current_active_user
from app.config.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/support", tags=["support"])

# How each category reads to people (notification titles, email subject lines). The frontend has
# its own copy with icons/colors in lib/supportCategories.ts.
CATEGORY_LABELS = {'support': 'Support question', 'bug': 'Bug report', 'feature': 'Feature idea'}

SCREENSHOT_PREFIX = "support-screenshots"
SCREENSHOT_MAX_BYTES = 10 * 1024 * 1024
MAX_SCREENSHOTS = 3

# The only keys a ticket's `details` may carry, each trimmed to a sane length - the blob comes
# straight from the browser, so nothing else is stored.
_DETAIL_KEYS = ('urgency', 'area', 'page_url', 'browser', 'os', 'device', 'screen', 'viewport',
                'timezone', 'language', 'user_agent')
_URGENCIES = ('low', 'normal', 'urgent')


def category_label(category: Optional[str]) -> str:
    return CATEGORY_LABELS.get(category or 'support', CATEGORY_LABELS['support'])


def _clean_details(raw: Optional[dict[str, Any]]) -> Optional[dict[str, str]]:
    if not raw:
        return None
    cleaned = {}
    for key in _DETAIL_KEYS:
        value = raw.get(key)
        if value is None or value == '':
            continue
        cleaned[key] = str(value)[:400 if key == 'user_agent' else 200]
    if cleaned.get('urgency') not in (None, *_URGENCIES):
        cleaned.pop('urgency')
    return cleaned or None


def _clean_attachments(urls: Optional[List[str]]) -> Optional[List[str]]:
    """Only screenshots this app uploaded itself (POST /screenshots) are accepted."""
    if not urls:
        return None
    allowed = f"https://{s3_service.PUBLIC_URL_HOST}/{SCREENSHOT_PREFIX}/"
    kept = [u for u in urls if isinstance(u, str) and u.startswith(allowed)][:MAX_SCREENSHOTS]
    return kept or None


def _is_unread_for_agent(ticket: SupportTicket) -> bool:
    if not ticket.messages:
        return False
    last_message = ticket.messages[-1]
    if last_message.sender_type != 'admin':
        return False
    if ticket.agent_last_read_at is None:
        return True
    return ticket.updated_at > ticket.agent_last_read_at


@router.post("/tickets", response_model=TicketDetail, status_code=status.HTTP_201_CREATED)
async def create_ticket(
    payload: TicketCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserModel = Depends(get_current_active_user),
):
    subject, body = payload.subject.strip(), payload.message.strip()
    if not subject or not body:
        raise HTTPException(status_code=422, detail="Please add a title and a message.")
    try:
        ticket = SupportTicket(
            agent_id=current_user.id, subject=subject[:200], status='open',
            category=payload.category, details=_clean_details(payload.details),
        )
        db.add(ticket)
        await db.flush()

        message = SupportTicketMessage(
            ticket_id=ticket.id, sender_type='agent', body=body,
            attachments=_clean_attachments(payload.attachments),
        )
        db.add(message)
        ticket.agent_last_read_at = datetime.now(timezone.utc)

        await db.commit()

        result = await db.execute(
            select(SupportTicket).options(selectinload(SupportTicket.messages)).where(SupportTicket.id == ticket.id)
        )
        return result.scalar_one()
    except Exception as e:
        logger.error("creating support ticket failed", extra={"error": str(e)})
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to create support ticket")


@router.post("/screenshots", response_model=ScreenshotUploadResponse)
async def upload_screenshot(
    file: UploadFile = File(...),
    current_user: UserModel = Depends(get_current_active_user),
):
    """A screenshot for a new ticket (bug reports mostly). Uploaded before the ticket is sent; the
    returned URL goes into TicketCreate.attachments. Saved as a JPEG like every other photo."""
    contents = await file.read()
    if len(contents) > SCREENSHOT_MAX_BYTES:
        raise HTTPException(status_code=422, detail="Screenshots must be 10MB or smaller.")
    try:
        jpeg = photo_to_jpeg(contents)
    except ValueError:
        raise HTTPException(status_code=422, detail="Please attach a JPG, PNG or WebP image.")
    try:
        url = s3_service.upload_bytes(jpeg, SCREENSHOT_PREFIX, ".jpg", "image/jpeg")
    except Exception as e:
        logger.error("uploading support screenshot failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Couldn't upload that screenshot - please try again.")
    return ScreenshotUploadResponse(url=url)


@router.get("/tickets", response_model=List[TicketListItem])
async def list_my_tickets(
    db: AsyncSession = Depends(get_db),
    current_user: UserModel = Depends(get_current_active_user),
):
    try:
        result = await db.execute(
            select(SupportTicket)
            .options(selectinload(SupportTicket.messages))
            .where(SupportTicket.agent_id == current_user.id)
            .order_by(SupportTicket.updated_at.desc())
        )
        tickets = result.scalars().all()
        return [
            TicketListItem(
                id=t.id, subject=t.subject, status=t.status, category=t.category or 'support',
                created_at=t.created_at, updated_at=t.updated_at,
                unread=_is_unread_for_agent(t),
            )
            for t in tickets
        ]
    except Exception as e:
        logger.error("listing support tickets failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to list support tickets")


@router.get("/tickets/unread-count", response_model=TicketUnreadCountResponse)
async def get_unread_count(
    db: AsyncSession = Depends(get_db),
    current_user: UserModel = Depends(get_current_active_user),
):
    try:
        result = await db.execute(
            select(SupportTicket)
            .options(selectinload(SupportTicket.messages))
            .where(SupportTicket.agent_id == current_user.id)
        )
        tickets = result.scalars().all()
        count = sum(1 for t in tickets if _is_unread_for_agent(t))
        return TicketUnreadCountResponse(unread_count=count)
    except Exception as e:
        logger.error("fetching support unread count failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to fetch unread count")


async def _get_owned_ticket(db: AsyncSession, ticket_id: str, agent_id: str) -> SupportTicket:
    result = await db.execute(
        select(SupportTicket)
        .options(selectinload(SupportTicket.messages))
        .where(and_(SupportTicket.id == ticket_id, SupportTicket.agent_id == agent_id))
    )
    ticket = result.scalar_one_or_none()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


@router.get("/tickets/{ticket_id}", response_model=TicketDetail)
async def get_ticket(
    ticket_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserModel = Depends(get_current_active_user),
):
    try:
        ticket = await _get_owned_ticket(db, ticket_id, current_user.id)
        ticket.agent_last_read_at = datetime.now(timezone.utc)
        await db.commit()
        return await _get_owned_ticket(db, ticket_id, current_user.id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("fetching support ticket failed", extra={"error": str(e)})
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to fetch ticket")


@router.post("/tickets/{ticket_id}/messages", response_model=TicketDetail)
async def reply_to_ticket(
    ticket_id: str,
    payload: MessageCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserModel = Depends(get_current_active_user),
):
    try:
        ticket = await _get_owned_ticket(db, ticket_id, current_user.id)

        message = SupportTicketMessage(ticket_id=ticket.id, sender_type='agent', body=payload.body)
        db.add(message)

        now = datetime.now(timezone.utc)
        ticket.status = 'open'
        ticket.updated_at = now
        ticket.agent_last_read_at = now

        await db.commit()
        return await _get_owned_ticket(db, ticket_id, current_user.id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("replying to support ticket failed", extra={"error": str(e)})
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to send reply")


@router.patch("/tickets/{ticket_id}/status", response_model=TicketDetail)
async def update_my_ticket_status(
    ticket_id: str,
    payload: TicketStatusUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserModel = Depends(get_current_active_user),
):
    """Lets an agent close their own ticket (or reopen it without needing to send a message
    first) - the admin-side equivalent in admin_support_routes.py is unrestricted, but this one
    is scoped to the agent's own tickets via _get_owned_ticket."""
    if payload.status not in ('open', 'closed'):
        raise HTTPException(status_code=400, detail="status must be 'open' or 'closed'")
    try:
        ticket = await _get_owned_ticket(db, ticket_id, current_user.id)
        ticket.status = payload.status
        await db.commit()
        return await _get_owned_ticket(db, ticket_id, current_user.id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("updating own support ticket status failed", extra={"error": str(e)})
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to update ticket status")
