"""Unusual interest from a lead - worth a quiet heads-up and a check-in (owner, 11/13).

"Someone unrepresented opening my email 3 times and clicking it twice - I wish I'd been told."
When a lead who doesn't have an agent of their own (represented 'no' or 'unknown', not signed)
opens the same email 2+ times or clicks it 2+ times, the agent hears about it in a subtle way:

- one bell row per person per day ("Jake opened your email 3 times"), updated as counts climb but
  never re-buzzing (LEAD_INTEREST - the Rundown lists it under Leads & sign-ins);
- a "Worth a check-in" card in the Daily Briefing and on that lead's page, saying what they opened,
  which homes it was about (or which homes they looked at after it), and a suggested follow-up
  that one tap adds to their follow-ups / calendar (add_followup) - or Dismiss (clear).

A nudge goes away once the agent adds the follow-up, dismisses it, reaches out (last_contacted_at
after the opens), or already has a follow-up open for that lead. Read-only except those taps.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import and_, or_, select

from app.config.logging import get_logger
from app.models.database import AgentTask, ClientEmail, Collection, Lead, Notification, Property, PropertyInteraction, User
from app.services.tasks_service import EASTERN

logger = get_logger(__name__)

REPEAT = 2               # 2nd open / 2nd click of the same email = worth telling
WINDOW = timedelta(days=3)
NOTE_TYPE = "LEAD_INTEREST"
TASK_SOURCE = "email_interest"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _first(name: Optional[str], email: str) -> str:
    n = (name or "").strip().split(" ")[0]
    return n[:1].upper() + n[1:] if n else email


def _times(n: int) -> str:
    return "twice" if n == 2 else f"{n} times"


def counts_line(opens: int, clicks: int, obj: str = "it") -> str:
    """"opened your email 3 times and clicked twice" / "clicked your email twice"."""
    if opens >= 1:
        line = f"opened {obj} {_times(opens)}" if opens > 1 else f"opened {obj}"
        if clicks >= 1:
            line += f" and clicked {_times(clicks)}" if clicks > 1 else " and clicked a link"
        return line
    return f"clicked {obj} {_times(clicks)}" if clicks > 1 else f"clicked {obj}"


def homes_line(homes: list[dict]) -> str:
    names = [str(h.get("street_address") or "").split(",")[0].strip() for h in homes if h.get("street_address")]
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{names[0]}, {names[1]} and {len(names) - 2} more"


def _eligible(lead: Optional[Lead]) -> bool:
    if lead is None or lead.hidden_at is not None:
        return False
    if (lead.represented or "").lower() == "yes" or (lead.status or "") == "signed":
        return False   # their own agent / already the agent's client - not "unusual"
    return True


def _agent_words(lead: Lead) -> str:
    return "No agent" if (lead.represented or "").lower() == "no" else "Not sure if they have an agent"


def suggest_due(now: Optional[datetime] = None) -> datetime:
    """Before noon: this afternoon at 3. Otherwise tomorrow at 10 AM (Monday if that's a Sunday)."""
    local = (now or _now()).astimezone(EASTERN)
    if local.hour < 12:
        due = datetime.combine(local.date(), time(15, 0), EASTERN)
    else:
        d = local.date() + timedelta(days=1)
        if d.weekday() == 6:
            d += timedelta(days=1)
        due = datetime.combine(d, time(10, 0), EASTERN)
    return due.astimezone(timezone.utc)


def _when_label(due: datetime, now: Optional[datetime] = None) -> str:
    local, today = due.astimezone(EASTERN), (now or _now()).astimezone(EASTERN).date()
    hour = local.strftime("%I").lstrip("0") + (f":{local.minute:02d}" if local.minute else "") + (" AM" if local.hour < 12 else " PM")
    day = "today" if local.date() == today else "tomorrow" if local.date() == today + timedelta(days=1) else local.strftime("%A")
    return f"{day} at {hour}"


async def _viewed_after(db, owner_id: str, email: str, since: Optional[datetime], limit: int = 4) -> list[dict]:
    """Homes this person looked at on their Showcase after the email went out - what the email led
    to, for emails sent before we stored what they were about."""
    if not since:
        return []
    rows = (await db.execute(
        select(Property, PropertyInteraction.liked, PropertyInteraction.view_count)
        .join(PropertyInteraction, PropertyInteraction.property_id == Property.id)
        .join(Collection, Collection.id == PropertyInteraction.collection_id)
        .where(Collection.owner_id == owner_id, Collection.visitor_email.ilike(email),
               PropertyInteraction.last_viewed_at >= since)
        .order_by(PropertyInteraction.liked.desc().nullslast(), PropertyInteraction.last_viewed_at.desc())
        .limit(limit)
    )).all()
    out = []
    for p, liked, views in rows:
        out.append({"street_address": p.street_address or "", "city": (p.city or "").title(), "image": p.img_src or "",
                    "price": f"${p.price:,.0f}" if p.price else "", "beds": p.bedrooms, "baths": p.bathrooms,
                    "liked": bool(liked), "views": int(views or 0), "property_id": p.id})
    return out


def _label(rec: ClientEmail) -> str:
    from app.services.client_email_service import template_label
    return template_label(rec.template)


async def _signal(db, rec: ClientEmail, lead: Lead, now: datetime) -> dict[str, Any]:
    about = list(rec.about or [])
    viewed = [] if about else await _viewed_after(db, rec.owner_id, rec.recipient_email, rec.sent_at)
    homes = about or viewed
    who = _first(lead.name, rec.recipient_email)
    opens, clicks = int(rec.open_count or 0), int(rec.click_count or 0)
    label = _label(rec)
    where = homes_line(homes)
    due = suggest_due(now)
    what = f"{who} {counts_line(opens, clicks, f'your {label}')}"
    why = what + (f" - about {where}" if where and about else f" - then looked at {where}" if where else "") + "."
    first_home = where.split(",")[0].split(" and ")[0] if where else ""
    notes = (f"{why} {_agent_words(lead)}. Check in while they're interested - "
             f"{'ask if they want to see ' + first_home + ' in person' if first_home else 'ask what caught their eye'}.")
    text = (f"Hi {who}, just checking in - " + (f"would you like to see {first_home} in person? " if first_home else "anything catching your eye lately? ")
            + "I can set up a time that works for you.")
    return {
        "email_id": rec.id, "lead_id": lead.id, "name": lead.name or who, "first_name": who,
        "represented": lead.represented or "unknown", "agent_words": _agent_words(lead),
        "label": label, "subject": rec.subject or "", "template": rec.template,
        "opens": opens, "clicks": clicks, "sent_at": rec.sent_at, "last_at": rec.last_engaged_at or rec.clicked_at or rec.opened_at,
        "about": about, "viewed_after": viewed, "homes_line": where,
        "headline": what, "why": why,
        "suggestion": {"title": f"Check in with {who} - {_times(max(opens, clicks))} on your {label}",
                       "due_at": due, "when_label": _when_label(due, now), "notes": notes, "message": text, "kind": "text"},
    }


async def signals(db, owner_id: str, *, lead_id: Optional[str] = None, now: Optional[datetime] = None, limit: int = 12) -> list[dict]:
    """The open "worth a check-in" nudges, newest first, one per lead (their strongest email)."""
    now = now or _now()
    since = now - WINDOW
    lead_q = select(Lead).where(Lead.owner_id == owner_id, Lead.email.isnot(None), Lead.hidden_at.is_(None))
    if lead_id:
        lead_q = lead_q.where(Lead.id == lead_id)
    rows = (await db.execute(
        select(ClientEmail, Lead)
        .join(Lead, and_(Lead.owner_id == ClientEmail.owner_id, Lead.email == ClientEmail.recipient_email))
        .where(ClientEmail.owner_id == owner_id, ClientEmail.status == "sent",
               ClientEmail.last_engaged_at >= since,
               or_(ClientEmail.open_count >= REPEAT, ClientEmail.click_count >= REPEAT),
               or_(ClientEmail.nudge_cleared_at.is_(None), ClientEmail.nudge_cleared_at < ClientEmail.last_engaged_at),
               Lead.id.in_(lead_q.with_only_columns(Lead.id)))
        .order_by(ClientEmail.last_engaged_at.desc())
    )).all()
    best: dict[str, tuple[ClientEmail, Lead]] = {}
    for rec, lead in rows:
        if not _eligible(lead):
            continue
        if lead.last_contacted_at and rec.last_engaged_at and lead.last_contacted_at > rec.last_engaged_at:
            continue   # they already reached out after it
        cur = best.get(lead.id)
        score = (rec.open_count or 0) + 2 * (rec.click_count or 0)
        if cur is None or score > (cur[0].open_count or 0) + 2 * (cur[0].click_count or 0):
            best[lead.id] = (rec, lead)
    if not best:
        return []
    # Already handled: a follow-up open for that lead, or one made from this nudge.
    lead_ids = list(best)
    busy = {lid for (lid,) in (await db.execute(select(AgentTask.lead_id).where(
        AgentTask.owner_id == owner_id, AgentTask.lead_id.in_(lead_ids), AgentTask.completed_at.is_(None)))).all()}
    made = {sid for (sid,) in (await db.execute(select(AgentTask.auto_source_id).where(
        AgentTask.owner_id == owner_id, AgentTask.auto_source == TASK_SOURCE,
        AgentTask.auto_source_id.in_([r.id for r, _ in best.values()])))).all()}
    out = []
    for lid, (rec, lead) in best.items():
        if lid in busy or rec.id in made:
            continue
        out.append(await _signal(db, rec, lead, now))
    out.sort(key=lambda s: s["last_at"] or now, reverse=True)
    return out[:limit]


async def note_repeat(db, rec: ClientEmail) -> None:
    """The quiet bell row - one per person per day, updated as the counts climb, never re-buzzing."""
    from app.services.client_email_service import _lead, _person_name
    lead = await _lead(db, rec.owner_id, rec.recipient_email)
    if not _eligible(lead):
        return
    opens, clicks = int(rec.open_count or 0), int(rec.click_count or 0)
    if opens < REPEAT and clicks < REPEAT:
        return
    name = await _person_name(db, rec.owner_id, rec.recipient_email) or lead.name
    who = _first(name, rec.recipient_email)
    day = _now().astimezone(EASTERN).date().isoformat()
    ref = f"{rec.recipient_email}|{day}"
    n = (await db.execute(select(Notification).where(
        Notification.agent_id == rec.owner_id, Notification.type == NOTE_TYPE, Notification.reference_id == ref).limit(1))).scalar_one_or_none()
    homes = homes_line(rec.about or [])
    title = f"{who} {counts_line(opens, clicks, 'your email')}"
    message = (f"Your {_label(rec)}" + (f" - about {homes}" if homes else "") + f". {_agent_words(lead)} - worth a check-in.")
    link = f"/leads?lead={lead.id}"
    if n is None:
        db.add(Notification(agent_id=rec.owner_id, type=NOTE_TYPE, reference_type="CLIENT_EMAIL", reference_id=ref,
                            title=title, message=message, visitor_name=name or None, link=link, is_read=False, created_at=_now()))
    else:
        n.title, n.message, n.link = title, message, link
    await db.commit()


async def _owned_email(db, owner_id: str, email_id: str) -> ClientEmail:
    rec = await db.get(ClientEmail, email_id)
    if rec is None or rec.owner_id != owner_id:
        raise LookupError("email not found")
    return rec


async def clear(db, user: User, email_id: str) -> None:
    rec = await _owned_email(db, user.id, email_id)
    rec.nudge_cleared_at = _now()
    await db.commit()


async def add_followup(db, user: User, email_id: str, *, due_at: Optional[datetime] = None) -> AgentTask:
    """One tap: the suggested check-in becomes a follow-up (in their calendar feed / synced calendar)."""
    from app.services import calendar_sync_service, tasks_service
    from app.services.client_email_service import _lead
    rec = await _owned_email(db, user.id, email_id)
    lead = await _lead(db, user.id, rec.recipient_email)
    if lead is None:
        raise LookupError("lead not found")
    existing = (await db.execute(select(AgentTask).where(
        AgentTask.owner_id == user.id, AgentTask.auto_source == TASK_SOURCE, AgentTask.auto_source_id == rec.id).limit(1))).scalar_one_or_none()
    if existing is not None:
        rec.nudge_cleared_at = _now()
        await db.commit()
        return existing
    s = await _signal(db, rec, lead, _now())
    due = due_at or s["suggestion"]["due_at"]
    task = AgentTask(owner_id=user.id, lead_id=lead.id, title=s["suggestion"]["title"], notes=s["suggestion"]["notes"],
                     kind="text", priority="high", due_at=due, remind_at=due, auto_source=TASK_SOURCE, auto_source_id=rec.id,
                     suggested_message=tasks_service.with_footer(s["suggestion"]["message"], user))
    db.add(task)
    rec.nudge_cleared_at = _now()
    await db.commit()
    try:
        calendar_sync_service.sync_task_bg(user.id, task.id)
    except Exception:  # noqa: BLE001
        logger.warning("calendar sync for interest follow-up failed", exc_info=True)
    return task
