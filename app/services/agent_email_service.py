"""Notification emails - the email side of the in-app bell (Mailgun templates; every template, its
variables and its HTML are in the "Mailgun Template Ledger" artifact).

One entry point per audience:
- email_agent(user, notification_type, template, subject, variables) - to an agent, only when their
  Settings -> Notifications email switch for that kind of event is on (always=True for account
  and billing notices, which aren't optional).
- email_person(to, template, subject, variables) - to a buyer / guest.

Both are fire-and-forget: the Mailgun call runs off the event loop and every failure is swallowed
and logged, so an email can never slow down or break the action that triggered it. MAILGUN_DEV=yes
(dev) only logs them."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any, Optional

from app.config.logging import get_logger

logger = get_logger(__name__)


def app_url() -> str:
    base = os.getenv("FRONTEND_URL") or os.getenv("CLIENT_URL")
    if not base:
        base = "http://localhost:3000" if os.getenv("DEV", "false") == "true" else "https://openhousepal.com"
    return base.rstrip("/")


def absolute(link: Optional[str]) -> str:
    """"/leads/123" -> "https://openhousepal.com/leads/123"; full URLs pass through."""
    if not link:
        return app_url()
    return link if link.startswith("http") else f"{app_url()}{link if link.startswith('/') else '/' + link}"


async def agent_public_phone(db, user) -> str:
    """The phone number guests see for this agent - their Agent Hub "Call" number, formatted
    "(610) 555-0101". Agents have no phone on the account itself (emails used to read a missing
    `user.phone` and always showed a blank). "" when none is set; never raises."""
    if user is None:
        return ""
    try:
        from app.services.website import content_adapters
        raw = str((await content_adapters.get_agent_profile(db, user)).get("phone") or "")
    except Exception:  # noqa: BLE001
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else raw.strip()


def full_name(user) -> str:
    return f"{getattr(user, 'first_name', '') or ''} {getattr(user, 'last_name', '') or ''}".strip() or "there"


def nice_date(d: Optional[datetime]) -> str:
    if not d:
        return ""
    return f"{d.strftime('%A, %B')} {d.day}"


def _send_full(to: str, subject: str, template: str, variables: dict, reply_to: Optional[str], from_name: str) -> tuple[int, str]:
    try:
        from app.services.email_service import EmailService
        code, body = EmailService().send_simple_message(
            to_email=to, subject=subject, template=template, template_variables=variables,
            reply_to=reply_to, from_name=from_name,
        )
        if code >= 300:
            logger.warning("notification email not accepted", extra={"template": template, "status": code, "body": str(body)[:300]})
        return code, str(body or "")
    except Exception as e:  # noqa: BLE001 - an email must never break anything
        logger.warning("notification email failed", exc_info=True, extra={"template": template})
        return 500, str(e)


def _send(to: str, subject: str, template: str, variables: dict, reply_to: Optional[str], from_name: str) -> int:
    return _send_full(to, subject, template, variables, reply_to, from_name)[0]


async def _send_via_owner(owner_id: str, to: str, subject: str, template: str, variables: dict,
                          reply_to: Optional[str], from_name: str) -> None:
    """A guest email: from the agent's own mailbox when they connected one, else OpenHousePal.
    Tracked and checked against the engagement rules first (client_email_service) - an automatic
    update to someone who stopped opening them is held back."""
    from app.services.agent_mailbox_service import send_guest
    from app.services.client_email_service import after_send, before_send, strip_private
    gate = await before_send(owner_id, to, template, subject, variables)
    if gate is None:
        return
    track_id, variables = gate
    sent = await send_guest(owner_id, to, subject, template, strip_private(variables),
                            from_name=None if from_name == "OpenHousePal" else from_name)
    if sent is None:
        code, body = await asyncio.get_running_loop().run_in_executor(None, _send_full, to, subject, template, variables, reply_to, from_name)
        await after_send(track_id, code < 300, "openhousepal", code=code, detail=body)
    else:
        await after_send(track_id, sent[0] < 300, "mailbox", code=sent[0], detail=sent[1])


def send(to: Optional[str], template: str, subject: str, variables: dict[str, Any], *,
         reply_to: Optional[str] = None, from_name: str = "OpenHousePal", owner_id: Optional[str] = None) -> None:
    """owner_id = the agent whose guest this is; set it only on emails TO guests/clients, so they can
    go out from that agent's own mailbox (agent_mailbox_service). Agent alerts never pass it."""
    if not to:
        return
    variables = {"app_url": app_url(), **{k: v for k, v in variables.items() if v is not None}}
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and owner_id:
        from app.services.agent_mailbox_service import spawn
        spawn(_send_via_owner(owner_id, to, subject, template, variables, reply_to, from_name))
    elif loop is not None:
        loop.run_in_executor(None, _send, to, subject, template, variables, reply_to, from_name)
    else:
        _send(to, subject, template, variables, reply_to, from_name)


def send_simple_bg(to_email: Optional[str] = None, subject: str = "", template: str = "", template_variables: Optional[dict] = None,
                   reply_to: Optional[str] = None, from_name: str = "OpenHousePal", owner_id: Optional[str] = None) -> None:
    """Drop-in for EmailService().send_simple_message(...) inside a request handler: same keyword
    arguments, but sent in the background so a slow Mailgun never holds up the guest's request."""
    send(to_email, template, subject, template_variables or {}, reply_to=reply_to, from_name=from_name, owner_id=owner_id)


def email_agent(user, notification_type: Optional[str], template: str, subject: str,
                variables: Optional[dict[str, Any]] = None, *, always: bool = False,
                reply_to: Optional[str] = None) -> None:
    """Email an agent about something that just happened, if they want that kind of email."""
    try:
        if user is None or not getattr(user, "email", None):
            return
        if not always and notification_type:
            from app.services.notification_preferences_service import channel_enabled
            if not channel_enabled(user, notification_type, "email"):
                return
        base = {"agent_name": full_name(user), "first_name": getattr(user, "first_name", None) or "there",
                "_log_owner": getattr(user, "id", None), "_log_audience": "agent"}
        merged = {**base, **(variables or {})}
        uid = getattr(user, "id", None)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if uid and loop is not None:
            # Open tracking + the "are they opening our emails?" rules (agent_email_health_service):
            # an agent on weekly gets non-critical ones in one weekly summary instead.
            import uuid as _uuid
            from app.services import agent_email_health_service as health
            from app.services.agent_mailbox_service import spawn
            log_id = str(_uuid.uuid4())
            merged = health.with_tracking(log_id, {**merged, "_log_id": log_id})
            spawn(health.send_gated(uid, user.email, template, subject, merged, reply_to, always))
            return
        send(user.email, template, subject, merged, reply_to=reply_to)
    except Exception:  # noqa: BLE001
        logger.warning("email_agent failed", exc_info=True, extra={"template": template})


def email_person(to: Optional[str], template: str, subject: str, variables: dict[str, Any], *,
                 reply_to: Optional[str] = None, from_name: str = "OpenHousePal", owner_id: Optional[str] = None) -> None:
    """Email a buyer / guest (from the agent's name when one is given; from the agent's own
    mailbox when owner_id is given and they connected one)."""
    try:
        send(to, template, subject, {**variables, "_log_owner": owner_id, "_log_audience": "client"},
             reply_to=reply_to, from_name=from_name, owner_id=owner_id)
    except Exception:  # noqa: BLE001
        logger.warning("email_person failed", exc_info=True, extra={"template": template})


PLAN_NAMES = {"NO_MLS": "No-MLS Basic", "PRO_NO_MLS": "No-MLS Premium", "BASIC": "Basic", "PREMIUM": "Premium", "IDX": "IDX"}


def plan_name(tier: Optional[str]) -> str:
    return PLAN_NAMES.get(tier or "", (tier or "your plan").replace("_", " ").title())
