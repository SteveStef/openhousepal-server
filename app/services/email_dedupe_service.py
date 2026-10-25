"""One email of each kind per person per event per day (owner, 10/25 - guard rail #3).

Stops repeats like several "Tomorrow: <invite>" emails to the same agent, or several tour
confirmations to the same client for the same tour, on the same day. Mailbox providers count
repeats against our sender score.

The key is (recipient, template, the event it's about, Eastern calendar day), hashed - no address
is ever stored. It lands on the email's email_log row (dedupe_key). Before an email goes out we look
for a row with the same key; if there is one, the repeat is NOT sent and is logged as its own row
(status "held", via "dedupe") so the admin Email health page shows it.

Only the kinds in RULES are checked, each with the variables that name its event. Two different
invites / tours / homes / Showcases always have different keys, so they are never blocked. Kinds
where every email is a new thing (a new lead, a form response, a comment) and every security /
account email (codes, password resets, billing) are not in RULES and are never touched. If an
email has none of its event variables, it's sent (we never guess).

Checked from the async send paths: agent_email_health_service.send_gated (agent emails) and
client_email_service.before_send (client emails, Mailgun or the agent's own mailbox)."""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# template -> the variables that say which event it's about ("*" = one a day, whatever it's about).
RULES: dict[str, tuple[str, ...]] = {
    # to agents
    "invite_day_before_agent": ("link", "day_label"),           # one per invite per day
    "invite_rsvp_agent": ("link", "guest_email", "guest_name", "rsvp_response", "guest_count"),  # the same RSVP twice
    "open_house_recap": ("link", "property_address"),           # one wrap-up per open house
    "showcase_daily_digest_agent": ("*",),                      # the daily briefing
    "spotlight_weekly_report": ("*",),
    # to clients
    "tour_confirmed": ("_subject",),                            # home + date + time are in the subject
    "visitor_confirmation": ("property_address", "test_property_address", "open_house_id"),
    "event_rsvp": ("link", "_subject"),                         # invite + their answer
    "showcase_ready": ("collection_link", "showcase_link"),
    "showcase_shared": ("collection_link", "showcase_link"),
    "showcase_daily_digest": ("collection_link", "showcase_link"),
    "showcase_weekly_digest": ("collection_link", "showcase_link"),
    "spotlight_alert_confirm": ("_subject", "unsubscribe_url"),
    "spotlight_documents": ("_subject",),
}
# Never checked, even if someone adds them above by mistake.
NEVER = frozenset({"verify_code", "password_reset", "account_approved", "agent_welcome", "payment_failed",
                   "subscription_cancelled", "subscription_suspended", "subscription_plan_changed", "trial_ending_soon"})

_recent: dict[str, float] = {}   # key -> when, this process (closes the gap before the log row is written)
_lock = threading.Lock()
_TTL = 26 * 3600


def _day(now: Optional[datetime] = None) -> str:
    from app.services.tasks_service import EASTERN
    return (now or datetime.now(timezone.utc)).astimezone(EASTERN).date().isoformat()


def _val(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    return str(v)


def key_for(to: Optional[str], template: str, subject: str, variables: dict, now: Optional[datetime] = None) -> Optional[str]:
    """The dedupe key, or None = don't check this one."""
    if not to or template in NEVER or template not in RULES:
        return None
    fields = RULES[template]
    if fields == ("*",):
        event = "*"
    else:
        vals = [_val(subject if f == "_subject" else (variables or {}).get(f)) for f in fields]
        if not any(vals):
            return None
        event = "|".join(vals)
    raw = f"{to.strip().lower()}\x1f{template}\x1f{event}\x1f{_day(now)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


def _seen_here(key: str) -> bool:
    now = time.monotonic()
    with _lock:
        for k in [k for k, t in _recent.items() if now - t > _TTL]:
            _recent.pop(k, None)
        if key in _recent:
            return True
        _recent[key] = now
        return False


async def is_repeat(key: Optional[str], template: str, audience: str, owner_id: Optional[str]) -> bool:
    """True = the same email already went to this person today: don't send; it's logged as held."""
    if not key:
        return False
    try:
        repeat = _seen_here(key)
        if not repeat:
            from sqlalchemy import select
            from app.database import AsyncSessionLocal
            from app.models.database import EmailLog
            from app.services import email_log_service
            await email_log_service.flush()
            async with AsyncSessionLocal() as db:
                repeat = (await db.execute(select(EmailLog.id).where(
                    EmailLog.dedupe_key == key, EmailLog.via != "dedupe", EmailLog.status != "failed").limit(1))).first() is not None
        if repeat:
            from app.services import email_log_service
            email_log_service.record(template, audience, held=True, owner_id=owner_id, via="dedupe", dedupe_key=key)
            logger.info("repeat email held (one per person per event per day)", extra={"template": template, "owner_id": owner_id})
        return repeat
    except Exception:  # noqa: BLE001 - never stop an email because of this check
        logger.warning("email dedupe check failed - sending", exc_info=True)
        return False


def forget(key: Optional[str]) -> None:
    """The send failed - a retry later today must not be blocked."""
    if key:
        with _lock:
            _recent.pop(key, None)
