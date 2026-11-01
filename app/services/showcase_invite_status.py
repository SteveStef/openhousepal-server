"""Did this client ever get their Showcase invite? (owner, 11/01: "the invite email is pretty
important" - a lead who never got it should be obvious, with a Send invite button.)

The invite is whichever email first carried their Showcase link: the open-house thank-you
(visitor_confirmation, showcase_link), "Your Showcase is ready" (showcase_ready) or the agent's
"Some homes I picked out for you" (showcase_shared) - both collection_link. Every one goes through
the email queue (ScheduledEmail), which keeps what happened to it: SENT, PENDING, CANCELLED (held
back by the email rules) or FAILED. Opens/clicks live on the lead's Email section, not here."""
from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import ScheduledEmail

INVITE_TEMPLATES = ("visitor_confirmation", "showcase_ready", "showcase_shared")
LINK_KEYS = ("collection_link", "showcase_link")


def _get(c: Any, k: str):
    return c.get(k) if isinstance(c, dict) else getattr(c, k, None)


def _links_to(row: ScheduledEmail, code: str | None, token: str | None) -> bool:
    v = row.template_variables or {}
    for k in LINK_KEYS:
        link = str(v.get(k) or "").split("?")[0].rstrip("/")
        if link and ((code and link.endswith(f"/{code}")) or (token and token in link)):
            return True
    return False


def _held_why(row: ScheduledEmail) -> str:
    # The queue writes one note for every hold (email_scheduler_service): a same-day repeat, the
    # client's email rules, or a Studio -> Email switch the agent turned off.
    return "Held back by your email settings"


async def invite_status(db: AsyncSession, cols: Iterable[Any]) -> dict[str, dict]:
    """{collection id: {"state": sent | sending | held | failed | never, "at": iso or None,
    "why": str or None, "can_send": bool, "blocked": str or None}} for Collections or the dicts
    get_user_collections returns. One query for all of them."""
    cols = [c for c in cols if _get(c, "id")]
    emails = {(_get(c, "visitor_email") or "").strip().lower() for c in cols} - {""}
    owners = {_get(c, "owner_id") for c in cols} - {None}
    rows: list[ScheduledEmail] = []
    if emails:
        stmt = select(ScheduledEmail).where(
            ScheduledEmail.template_name.in_(INVITE_TEMPLATES),
            func.lower(ScheduledEmail.recipient_email).in_(emails),
        )
        if owners:
            stmt = stmt.where(or_(ScheduledEmail.owner_id.in_(owners), ScheduledEmail.owner_id.is_(None)))
        rows = list((await db.execute(stmt.order_by(ScheduledEmail.created_at.desc()))).scalars().all())
    out: dict[str, dict] = {}
    for c in cols:
        email = (_get(c, "visitor_email") or "").strip().lower()
        code, token = _get(c, "short_code"), _get(c, "share_token")
        mine = [r for r in rows if (r.recipient_email or "").strip().lower() == email and _links_to(r, code, token)
                and (r.owner_id in (None, _get(c, "owner_id")) or not _get(c, "owner_id"))]
        sent = next((r for r in mine if r.status == "SENT"), None)
        if sent:
            st = {"state": "sent", "at": (sent.sent_at or sent.created_at).isoformat() if (sent.sent_at or sent.created_at) else None, "why": None}
        elif mine:
            last = mine[0]
            state = {"PENDING": "sending", "CANCELLED": "held", "FAILED": "failed"}.get(last.status or "", "failed")
            st = {"state": state, "at": last.created_at.isoformat() if last.created_at else None,
                  "why": _held_why(last) if state == "held" else ("It didn't go through" if state == "failed" else None)}
        else:
            st = {"state": "never", "at": None, "why": None}
        blocked = None
        if not email:
            blocked = "No email address"
        elif _get(c, "is_blacklisted"):
            blocked = "They unsubscribed from emails"
        elif _get(c, "notify_visitor") is False:
            blocked = "Showcase emails are off for them"
        elif (_get(c, "status") or "ACTIVE") != "ACTIVE":
            blocked = "This Showcase is asleep"
        out[str(_get(c, "id"))] = {**st, "can_send": blocked is None and st["state"] != "sending", "blocked": blocked}
    return out
