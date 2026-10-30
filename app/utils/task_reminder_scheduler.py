"""APScheduler entry point that fires agent-task reminders (an in-app Notification) when a
task's remind_at passes, even if the agent never opens the Leads page. Registered in
app/main.py's lifespan(). Same shape as the other *_scheduler.py wrappers.

Right after midnight (Eastern) it moves missed follow-ups onto the new day on the agent's calendar
("Overdue - Reach Out") - the calendar event only moves when it's re-pushed, so doing this at 6 AM
left yesterday's follow-ups sitting on yesterday all night.

Once each (Eastern) morning it also:
- deletes follow-ups completed more than COMPLETED_RETENTION_DAYS ago, so the list never stacks
  up with old checked-off items,
- clears the answers/contact details of unfinished form responses older than
  forms_service.UNFINISHED_RETENTION_DAYS (the visits still count in form stats).

Every minute it also sends any roll-ups that are due (agent_roundup_service): the hourly liked-homes
email. (Each open house's wrap-up goes in the agent's daily briefing since 10/30.)

And once each afternoon (4:30 PM Eastern) it sends the Showcase daily digest - every new match and
price drop of the day in one email per buyer (showcase_digest_service); each agent's daily briefing goes
out at the time they picked in Studio -> Email (4:30 PM by default).
"""
from datetime import date, datetime

from app.config.logging import get_logger
from app.database import AsyncSessionLocal
from app.services.tasks_service import (
    EASTERN, carry_forward_overdue, fire_due_reminders, purge_old_completed,
)

logger = get_logger(__name__)

MORNING_HOUR = 6  # Eastern - before the agent's day starts
_last_daily: date | None = None
_last_carry: date | None = None  # the just-after-midnight overdue move
_last_digest: date | None = None  # 4:30 PM Showcase digest (showcase_digest_service)


async def scheduled_task_reminders():
    global _last_daily, _last_digest, _last_carry
    try:
        async with AsyncSessionLocal() as db:
            fired = await fire_due_reminders(db)
            if fired:
                logger.info("Task reminders fired", extra={"event": "task_reminders", "count": fired})
            local = datetime.now(EASTERN)
            if _last_carry != local.date():
                _last_carry = local.date()
                moved = await carry_forward_overdue(db)
                logger.info("Overdue follow-ups moved to the new day", extra={"event": "task_carry", "carried_forward": moved})
            if _last_daily != local.date() and local.hour >= MORNING_HOUR:
                _last_daily = local.date()
                purged = await purge_old_completed(db)
                from app.services.forms_service import purge_old_unfinished
                cleared = await purge_old_unfinished(db)
                try:
                    from app.services.billing_notices_service import send_trial_ending_reminders
                    await send_trial_ending_reminders(db)
                except Exception:
                    logger.error("trial-ending reminders failed", exc_info=True)
                logger.info("Daily follow-up upkeep", extra={"event": "task_daily", "purged": purged,
                                                             "unfinished_cleared": cleared})
            try:
                from app.services.agent_roundup_service import send_like_roundups
                await send_like_roundups(db)  # open house wrap-ups ride in the daily briefing (10/30)
            except Exception:
                logger.error("agent roll-up emails failed", exc_info=True)
            try:
                from app.services.agent_email_health_service import send_weekly_summaries
                await send_weekly_summaries(db)  # agents who stopped opening our emails: one summary a week
            except Exception:
                logger.error("agent weekly email summaries failed", exc_info=True)
            try:
                from app.services.spotlight_alerts_service import send_daily_alerts, send_weekly_reports
                await send_daily_alerts(db)  # Spotlight guests: price / open house / status news (10 AM)
                await send_weekly_reports(db)  # agents: the Monday Spotlight report (8 AM)
            except Exception:
                logger.error("spotlight alerts / weekly report failed", exc_info=True)
            try:
                from app.services.spotlight_publish_service import sweep_pending
                await sweep_pending(db)  # Spotlights waiting to publish once their setup is done
            except Exception:
                logger.error("spotlight pending publish sweep failed", exc_info=True)
            from app.services.showcase_digest_service import is_digest_time, send_agent_briefings, send_showcase_digests
            if _last_digest != local.date() and is_digest_time():
                _last_digest = local.date()
                try:
                    await send_showcase_digests(db)   # buyers' 4:30 emails (+ any briefings due)
                except Exception:
                    logger.error("showcase daily digest failed", exc_info=True)
            else:
                try:
                    await send_agent_briefings(db)    # each agent at the time they picked
                except Exception:
                    logger.error("daily briefings failed", exc_info=True)
    except Exception as e:
        logger.error("Scheduled task reminders failed", exc_info=True, extra={"error": str(e)})
