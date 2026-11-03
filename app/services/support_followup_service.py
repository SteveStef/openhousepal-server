"""A new Help ticket puts a "check this ticket" follow-up on the owner's calendar the next day, like any
other follow-up (owner, 11/03: "so I always check the tickets"). It goes on the admin account
(ADMIN_EMAIL - the same account the admin bell alerts use), in a free quarter-hour of tomorrow's
8am-5pm, and is checked off by itself once the owner answers or closes the ticket.

Best-effort: a ticket is never refused because this failed."""
import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import or_, select

from app.config.logging import get_logger
from app.database import AsyncSessionLocal
from app.models.database import AgentTask, User

logger = get_logger(__name__)

SOURCE = "support_ticket"
CATEGORY_WORDS = {"support": "Help question", "bug": "Bug report", "feature": "Feature idea"}


def _admin_url(ticket_id: str) -> str:
    base = os.getenv("FRONTEND_URL", os.getenv("CLIENT_URL", "http://localhost:3000")).rstrip("/")
    return f"{base}/admin/inbox/support?ticket={ticket_id}"


def _for_ticket(ticket_id: str):
    # the first message's reminder carries the ticket id; one for a later agent reply "<ticket id>:<message id>"
    return or_(AgentTask.auto_source_id == ticket_id, AgentTask.auto_source_id.like(f"{ticket_id}:%"))


def agent_bits(user: User) -> dict:
    """Plain values, read before the route's commit - the background job runs after the request's
    session is gone, so it must never touch the User object."""
    return {"agent_id": user.id, "agent_name": " ".join(x for x in (user.first_name, user.last_name) if x), "agent_email": user.email or ""}


async def add_ticket_followup(ticket_id: str, subject: str, first_message: str, agent_id: str, agent_name: str, agent_email: str,
                              category: Optional[str] = None, reply_id: Optional[str] = None) -> None:
    """reply_id: the agent wrote again on a ticket - a new reminder unless one is still waiting."""
    from app.services import calendar_sync_service, tasks_service
    try:
        async with AsyncSessionLocal() as db:
            admin = (await db.execute(select(User).where(User.email == os.getenv("ADMIN_EMAIL", "admin@openhousepal.com")))).scalar_one_or_none()
            if not admin or admin.id == agent_id:
                return   # the owner testing their own Help page needs no reminder
            if (await db.execute(select(AgentTask.id).where(
                    AgentTask.owner_id == admin.id, AgentTask.auto_source == SOURCE, _for_ticket(ticket_id), AgentTask.completed_at.is_(None)))).first():
                return   # one is already waiting on the calendar for this ticket
            who = agent_name or agent_email or "An agent"
            kind = CATEGORY_WORDS.get(category or "", "Help ticket")
            if reply_id:
                kind = f"New reply on a {kind[0].lower() + kind[1:]}"
            start, end = tasks_service.followup_window("next_day", datetime.now(timezone.utc))
            due = await tasks_service._pick_slot(db, admin.id, start, end)
            body = (first_message or "").strip()
            task = AgentTask(
                owner_id=admin.id, kind="general", priority="high",
                title=f"Check Help ticket: {subject[:120]}",
                context=f"{kind} from {who}"[:250],
                notes=(f"{kind} from {who} ({agent_email})\n\n"
                       + (body[:600] + ("..." if len(body) > 600 else "") + "\n\n" if body else "")
                       + f"Open it: {_admin_url(ticket_id)}"),
                due_at=due, remind_at=due, all_day=False,
                auto_source=SOURCE, auto_source_id=f"{ticket_id}:{reply_id}" if reply_id else ticket_id,
            )
            db.add(task)
            await db.commit()
            await tasks_service.ensure_done_code(db, task)
            await db.commit()
            task_id, owner_id = task.id, admin.id
        calendar_sync_service.sync_task_bg(owner_id, task_id)
    except Exception:  # noqa: BLE001 - never block a ticket on its reminder
        logger.warning("Help ticket follow-up failed", exc_info=True, extra={"ticket_id": ticket_id})


async def complete_ticket_followup(ticket_id: str) -> None:
    """The owner answered or closed the ticket - its reminder is done (and leaves the calendar)."""
    from app.services import calendar_sync_service
    try:
        async with AsyncSessionLocal() as db:
            tasks = (await db.execute(select(AgentTask).where(
                AgentTask.auto_source == SOURCE, _for_ticket(ticket_id), AgentTask.completed_at.is_(None)))).scalars().all()
            if not tasks:
                return
            now = datetime.now(timezone.utc)
            for t in tasks:
                t.completed_at = now
            await db.commit()
            done = [(t.owner_id, t.id) for t in tasks]
        for owner_id, task_id in done:
            calendar_sync_service.sync_task_bg(owner_id, task_id)
    except Exception:  # noqa: BLE001
        logger.warning("Help ticket follow-up check-off failed", exc_info=True, extra={"ticket_id": ticket_id})
