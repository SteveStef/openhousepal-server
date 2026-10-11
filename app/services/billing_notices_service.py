"""Account notices that come from a date, not an event - run once each morning (see
utils/task_reminder_scheduler.py). Today: "your free trial ends in 3 days"."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import Notification, User

logger = get_logger(__name__)


async def send_trial_ending_reminders(db: AsyncSession) -> int:
    """Trials ending 2-3 days from now get one heads-up (bell + email). The bell row doubles as
    the "already told them" marker, so a restart that re-runs the morning job can't send twice."""
    from app.services.agent_email_service import absolute, email_agent, nice_date, plan_name
    now = datetime.now(timezone.utc)
    users = (await db.execute(
        select(User).where(
            User.subscription_status == "TRIAL",
            User.trial_ends_at >= now + timedelta(days=2),
            User.trial_ends_at < now + timedelta(days=3),
        )
    )).scalars().all()
    sent = 0
    for user in users:
        already = (await db.execute(
            select(Notification.id).where(
                Notification.agent_id == user.id, Notification.type == "TRIAL_ENDING_SOON",
                Notification.created_at >= now - timedelta(days=7),
            ).limit(1)
        )).scalar_one_or_none()
        if already:
            continue
        ends = nice_date(user.trial_ends_at)
        db.add(Notification(
            agent_id=user.id, type="TRIAL_ENDING_SOON", reference_type="ACCOUNT", reference_id=user.id,
            title="Your free trial ends in 3 days",
            message=f"Your {plan_name(user.plan_tier)} plan continues automatically on {ends}.",
            link="/settings#billing", is_read=False, created_at=now,
        ))
        await db.commit()
        email_agent(user, None, "trial_ending_soon", "Your OpenHousePal trial ends in 3 days",
                    {"trial_end_date": ends, "plan_name": plan_name(user.plan_tier), "link": absolute("/settings#billing")}, always=True)
        sent += 1
    return sent
