"""Admin-side support ticket inbox - list every agent's tickets, read a thread, reply, and
close/reopen. Replying creates a Notification for the agent (same mechanism every other
in-app alert uses - see notifications_routes.py / NOTIFICATION_META on the frontend) so the
agent sees it on the bell and the Activity page without a second notification pipeline.
"""
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.database import SupportTicket, SupportTicketMessage, Notification, User as UserModel
from app.schemas.support import (
    MessageCreate, TicketDetail, AdminTicketListItem, TicketStatusUpdate, TicketUnreadCountResponse,
)
from app.api.support_routes import category_label
from app.utils.auth import require_admin_user
from app.config.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/support", tags=["admin", "support"])


def _is_unread_for_admin(ticket: SupportTicket) -> bool:
    if not ticket.messages:
        return False
    last_message = ticket.messages[-1]
    if last_message.sender_type != 'agent':
        return False
    if ticket.admin_last_read_at is None:
        return True
    return ticket.updated_at > ticket.admin_last_read_at


@router.get("/tickets", response_model=List[AdminTicketListItem])
async def list_all_tickets(
    db: AsyncSession = Depends(get_db),
    admin: UserModel = Depends(require_admin_user),
):
    try:
        result = await db.execute(
            select(SupportTicket)
            .options(selectinload(SupportTicket.messages), selectinload(SupportTicket.agent))
            .order_by(SupportTicket.updated_at.desc())
        )
        tickets = result.scalars().all()
        items = [
            AdminTicketListItem(
                id=t.id, subject=t.subject, status=t.status, category=t.category or 'support',
                created_at=t.created_at, updated_at=t.updated_at,
                unread=_is_unread_for_admin(t),
                agent_id=t.agent_id,
                agent_name=f"{t.agent.first_name} {t.agent.last_name}".strip() if t.agent else "Unknown Agent",
                agent_email=t.agent.email if t.agent else "",
            )
            for t in tickets
        ]
        # Unread-for-admin first, then most recently active within each group.
        items.sort(key=lambda i: (not i.unread, -i.updated_at.timestamp()))
        return items
    except Exception as e:
        logger.error("listing all support tickets failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to list support tickets")


@router.get("/tickets/unread-count", response_model=TicketUnreadCountResponse)
async def get_admin_unread_count(
    db: AsyncSession = Depends(get_db),
    admin: UserModel = Depends(require_admin_user),
):
    try:
        result = await db.execute(select(SupportTicket).options(selectinload(SupportTicket.messages)))
        tickets = result.scalars().all()
        count = sum(1 for t in tickets if _is_unread_for_admin(t))
        return TicketUnreadCountResponse(unread_count=count)
    except Exception as e:
        logger.error("fetching admin support unread count failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Failed to fetch unread count")


async def _get_any_ticket(db: AsyncSession, ticket_id: str) -> SupportTicket:
    result = await db.execute(
        select(SupportTicket).options(selectinload(SupportTicket.messages)).where(SupportTicket.id == ticket_id)
    )
    ticket = result.scalar_one_or_none()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


@router.get("/tickets/{ticket_id}", response_model=TicketDetail)
async def get_ticket(
    ticket_id: str,
    db: AsyncSession = Depends(get_db),
    admin: UserModel = Depends(require_admin_user),
):
    try:
        ticket = await _get_any_ticket(db, ticket_id)
        ticket.admin_last_read_at = datetime.now(timezone.utc)
        await db.commit()
        return await _get_any_ticket(db, ticket_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("fetching support ticket (admin) failed", extra={"error": str(e)})
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to fetch ticket")


@router.post("/tickets/{ticket_id}/messages", response_model=TicketDetail)
async def reply_to_ticket(
    ticket_id: str,
    payload: MessageCreate,
    db: AsyncSession = Depends(get_db),
    admin: UserModel = Depends(require_admin_user),
):
    try:
        ticket = await _get_any_ticket(db, ticket_id)

        message = SupportTicketMessage(ticket_id=ticket.id, sender_type='admin', body=payload.body)
        db.add(message)

        now = datetime.now(timezone.utc)
        ticket.updated_at = now
        ticket.admin_last_read_at = now

        preview = payload.body if len(payload.body) <= 120 else payload.body[:117] + "..."
        # Name what the reply is about ("your bug report") so the alert and the email read right.
        label = category_label(ticket.category)
        db.add(Notification(
            agent_id=ticket.agent_id,
            type="SUPPORT_TICKET_REPLY",
            reference_type="SUPPORT_TICKET",
            reference_id=ticket.id,
            title=f"Support replied to your {label.lower()}: {ticket.subject}",
            message=preview,
            link=f"/settings/help?ticket={ticket.id}",
            is_read=False,
        ))

        await db.commit()
        from app.services.agent_email_service import absolute, email_agent
        email_agent(await db.get(UserModel, ticket.agent_id), "SUPPORT_TICKET_REPLY", "support_ticket_reply", f"Re: [{label}] {ticket.subject}",
                    {"ticket_subject": f"{label}: {ticket.subject}", "ticket_category": label, "reply_body": payload.body, "link": absolute(f"/settings/help?ticket={ticket.id}")})
        return await _get_any_ticket(db, ticket_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("replying to support ticket (admin) failed", extra={"error": str(e)})
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to send reply")


@router.patch("/tickets/{ticket_id}/status", response_model=TicketDetail)
async def update_ticket_status(
    ticket_id: str,
    payload: TicketStatusUpdate,
    db: AsyncSession = Depends(get_db),
    admin: UserModel = Depends(require_admin_user),
):
    if payload.status not in ('open', 'closed'):
        raise HTTPException(status_code=400, detail="status must be 'open' or 'closed'")
    try:
        ticket = await _get_any_ticket(db, ticket_id)
        ticket.status = payload.status
        await db.commit()
        return await _get_any_ticket(db, ticket_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("updating support ticket status failed", extra={"error": str(e)})
        await db.rollback()
        raise HTTPException(status_code=500, detail="Failed to update ticket status")
