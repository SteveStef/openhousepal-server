"""Two-way calendar sync orchestration - Google Calendar and Microsoft/Outlook Calendar, on the
same footing.

On connect (either provider) we create an "OpenHousePal" secondary calendar in the agent's account
and back-fill their open tasks (with a date) + upcoming scheduled open houses into it. After that,
every task create / edit / complete / delete and scheduled-open-house create / delete mirrors to
that calendar. `get_busy` reads primary-calendar busy blocks (merged across every connected
provider) so the follow-up scheduler can avoid clashes. An agent can connect Google, Microsoft,
both, or neither - every entrypoint below loops over whichever providers are actually connected.

Design notes:
- Only ever writes to the calendar WE created. Never their others.
- Every public entrypoint is best-effort: it swallows errors into connection.last_error and
  never raises into task/open-house CRUD. Slow provider calls run on their own DB session via
  asyncio.create_task so they can't add latency to or roll back the request that triggered them.
- Mirrors exactly what the .ics feed shows (see tasks_service.render_ics): open, dated tasks and
  scheduled open houses within a -30d..future window.
- Event bodies are built ONCE per entity in a neutral, Google-Calendar-shaped dict
  (_task_event_body / _oh_event_body / _tour_event_body); each provider client's insert_event/
  patch_event translates that same dict into its own API's shape (see
  microsoft_calendar_service._to_graph_event). This orchestration layer never branches on shape.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import (
    AgentTask, CalendarConnection, CalendarEventLink, Collection, OpenHouseEvent,
    OpenHouseHostRequest, OpenHouseListingNote, OpenHouseTeamBroadcast, Property, PropertyTour, User,
)
from app.services import google_calendar_service as gcal
from app.services import microsoft_calendar_service as mscal

logger = logging.getLogger(__name__)

# One client module per provider, sharing the same function names/shapes (is_configured,
# build_auth_url, exchange_code, refresh_access_token, revoke, get_userinfo, create_app_calendar,
# get_calendar, insert_event, patch_event, delete_event, free_busy) - adding a third provider is
# "write the client, add one line here."
PROVIDERS: dict = {"google": gcal, "microsoft": mscal}

# asyncio.create_task only holds a weak ref to a running task - keep our own so a background
# sync isn't garbage-collected mid-flight. Discarded on completion.
_PENDING: set = set()


def _spawn(coro) -> None:
    try:
        t = asyncio.create_task(coro)
    except RuntimeError:  # no running loop (e.g. a sync context) - just skip the sync
        coro.close()
        return
    _PENDING.add(t)
    t.add_done_callback(_PENDING.discard)


def _client_for(conn: CalendarConnection):
    return PROVIDERS[conn.provider]


# --------------------------------------------------------------------------- connection helpers

async def get_connection(db: AsyncSession, owner_id: str, provider: str = "google") -> Optional[CalendarConnection]:
    return (await db.execute(
        select(CalendarConnection).where(
            CalendarConnection.owner_id == owner_id, CalendarConnection.provider == provider
        )
    )).scalar_one_or_none()


async def get_connections(db: AsyncSession, owner_id: str) -> list[CalendarConnection]:
    return list((await db.execute(
        select(CalendarConnection).where(CalendarConnection.owner_id == owner_id)
    )).scalars().all())


async def _valid_access_token(db: AsyncSession, conn: CalendarConnection) -> str:
    """Return a live access token, refreshing (and persisting) it if it's within 60s of expiry."""
    client = _client_for(conn)
    exp = conn.token_expiry
    if exp and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if conn.access_token and exp and exp - datetime.now(timezone.utc) > timedelta(seconds=60):
        return conn.access_token
    if not conn.refresh_token:
        raise RuntimeError("no refresh token - agent must reconnect")
    fresh = await client.refresh_access_token(conn.refresh_token)
    conn.access_token = fresh["access_token"]
    conn.token_expiry = fresh["expiry"]
    if fresh.get("refresh_token"):  # Microsoft may rotate it; Google generally doesn't reissue
        conn.refresh_token = fresh["refresh_token"]
    db.add(conn)
    await db.commit()
    return conn.access_token


_RECONNECT_SIGNS = ("invalid_grant", "no refresh token", "reconnect", "unauthorized", "invalid credentials", "token has been expired or revoked")


def _needs_reconnect(conn: CalendarConnection, err: Optional[str]) -> bool:
    e = (err or "").lower()
    return conn.refresh_token is None or any(s in e for s in _RECONNECT_SIGNS)


async def _note_error(db: AsyncSession, conn: CalendarConnection, err: str) -> None:
    was_broken = bool(conn.last_error) and _needs_reconnect(conn, conn.last_error)
    conn.last_error = err[:500]
    db.add(conn)
    await db.commit()
    # Tell the agent once, the moment the provider stops accepting us - otherwise follow-ups just
    # quietly stop reaching their phone.
    if not was_broken and _needs_reconnect(conn, conn.last_error):
        try:
            from app.models.database import User
            from app.services.agent_email_service import absolute, email_agent
            provider = "Outlook Calendar" if conn.provider == "microsoft" else "Google Calendar"
            email_agent(await db.get(User, conn.owner_id), "CALENDAR_DISCONNECTED", "calendar_disconnected",
                        f"Your {provider} disconnected from OpenHousePal",
                        {"provider": provider, "link": absolute("/studio/calendar")})
        except Exception:  # noqa: BLE001
            logger.warning("calendar disconnect email failed", exc_info=True)


async def _note_ok(db: AsyncSession, conn: CalendarConnection) -> None:
    conn.last_error = None
    conn.last_sync_at = datetime.now(timezone.utc)
    db.add(conn)
    await db.commit()


# --------------------------------------------------------------------------- event bodies
# Neutral, Google-Calendar-shaped bodies - see the module docstring for why these stay
# provider-agnostic (Microsoft's client translates the same dict internally).

def _to_utc(dt: datetime) -> datetime:
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


# Bright MLS clock times (open-house scheduled_start_time/end_time, tour confirmed_time) are
# always Eastern wall-clock time - anchor here rather than stamp them as UTC.
EASTERN = ZoneInfo("America/New_York")


# Agents work in New York time (every Bright MLS agent today). Events are written as Eastern
# wall-clock time WITH the zone attached - sending bare UTC made Google/Outlook show the events
# (and the calendar) as GMT.
CALENDAR_TZ = "America/New_York"


# Separates the sections of a follow-up's event notes (plain text - no bold / underline possible).
_DIVIDER = "\u2501" * 16


def _tidy_subject(s: str) -> str:
    from app.services.email_service import _tidy_subject as tidy
    return tidy(s)


def _local(dt: datetime) -> dict:
    return {"dateTime": _to_utc(dt).astimezone(EASTERN).isoformat(), "timeZone": CALENDAR_TZ}


# Done events' title prefix - plain letters, small beside the bold to-dos.
# "✓" (U+2713, a text check mark - not the ✅ emoji, which made iPhone Calendar draw the done
# title in a bigger font, 10/19). The user missed the check on done follow-ups (10/22).
DONE_PREFIX = "✓ DONE · "


def _bold(text: str) -> str:
    """To-dos (follow-ups, tours, showings not done yet) get bold letters - see calendar_bold."""
    from app.services.tasks_service import calendar_bold
    return calendar_bold(text)


def _task_event_body(task: AgentTask, showcase: Optional[dict] = None, color: str = "5",
                     done_link: Optional[str] = None, owner=None, contact_link: Optional[str] = None,
                     your_notes: Optional[list] = None, lead_name: Optional[str] = None,
                     comments: Optional[list] = None, lead=None, details: Optional[list] = None,
                     prop: Optional[dict] = None, message_override: Optional[str] = None) -> dict:
    """Google-shaped event for a follow-up. The description is PLAIN TEXT on purpose: Google's web
    view renders HTML, but the iPhone Calendar app (and other synced clients) showed the raw tags
    ("<b><a href=...>&#9989;"). So no bold / italics / underline - sections are told apart with an
    emoji + CAPS heading, blank lines and ━ divider lines. Every client turns a bare URL on its own
    line into a tappable link, so links always get their own line.
    Order: done / undo link (what shows when the agent taps the alert) -> who they are + save to
    contacts -> why + the home (linked) -> what they told you -> Showcase comments -> your notes ->
    the ready-to-send text, last, so "copy everything below" is literally true.
    `message_override` = an already-assembled message (the settings page's live preview)."""
    from app.services.tasks_service import (ALERT_NOTE_RULES, FILL_IN_ANSWER, FILL_IN_TIME, NOTED_GUEST_SOURCE, SHOWING_RULES, _pretty_phone, carried_forward,
                                            full_message, reminder_overrides, task_day, task_prefix)
    due = _to_utc(task.due_at)
    done = task.completed_at is not None
    summary = f"[{task_prefix(task)}] {task.title}"
    if not done:
        summary = _bold(summary)  # still to do: bold, so it stands out over the small done ones
    first = (lead_name or "").strip() or "them"
    parts = []
    if done:
        # Checked off: the event stays as a record, grey and silent, "DONE ·" then the title.
        # Plain letters only: a cross-out (a combining stroke after every letter) and the ✅ emoji
        # both make the iPhone Calendar draw the title in a bigger fallback font - a done follow-up
        # stood out MORE than the ones still to do (user, 10/19).
        summary = DONE_PREFIX + summary
        when = task.completed_at.astimezone(EASTERN)
        stamp = f"{when:%a} {when:%b} {when.day} at {when.hour % 12 or 12}:{when:%M} {'AM' if when.hour < 12 else 'PM'}"
        parts.append(f"✅ DONE - checked off {stamp}" + (f"\n↩️ Checked it off by mistake? Tap to undo:\n{done_link}" if done_link else ""))
    elif done_link:
        parts.append(f"✅ Done reaching out? Tap to check it off:\n{done_link}")
    # Who they are, right under the done / undo link - and still there once it's checked off, so
    # the contact info and the one-tap "save to contacts" never disappear with the follow-up.
    if lead is not None or contact_link:
        who = [f"\U0001F464 {getattr(lead, 'name', None) or lead_name or 'Contact'}"]
        if getattr(lead, "phone", None):
            who.append(f"\U0001F4DE {_pretty_phone(lead.phone)}")
        if getattr(lead, "email", None):
            who.append(f"✉️ {lead.email}")
        if contact_link:
            who.append(f"\n\U0001F4C7 Save {first}'s contact + any notes on them here:\n{contact_link}")
        who.append(_DIVIDER)
        parts.append("\n".join(who))
    why = []
    context = _tidy_subject(task.context or "")
    if context:
        why.append(f"\U0001F4CC {context}")
    if prop and prop.get("came_from"):
        why.append(f"\U0001F9ED {prop['came_from']}")
    if prop and prop.get("address"):
        addr = _tidy_subject(prop["address"])
        # The address once: no home line when the "why" line already names it.
        street = addr.split(",")[0].strip().lower()
        if not (street and street in context.lower()):
            why.append(f"\U0001F3E0 {addr}")
        if prop.get("url"):
            # Their own Showcase link - safe to forward. Opened while signed in, the page knows it's
            # the agent: their clicks don't count as the client's, and it offers the agent view.
            label = f"Open {first}'s Showcase" if prop.get("where") == "showcase" else "See the listing"
            why.append(f"\U0001F449 {label}:\n{prop['url']}")
    if why:
        parts.append("\n".join(why))
    # Notes minus the auto-written lines the event already shows (the context line, contact info).
    skip = ("Auto-created", "Open house: ", "Contact: ")
    if task.auto_source in ALERT_NOTE_RULES:
        skip += ("Email: ", "Phone: ")  # shown in the contact block above
    notes = "\n".join(l for l in (task.notes or "").splitlines() if not l.startswith(skip)).strip()
    # A line of space between the heads-up and the agent's own note.
    notes = notes.replace("\nYour note: ", "\n\nYour note: ")
    if notes:
        parts.append(("\U0001F6A8 WHAT TO DO\n" if task.auto_source in ALERT_NOTE_RULES else "\U0001F5D2️ ") + notes)
    if details and not done:
        parts.append("\U0001F4CB WHAT THEY TOLD YOU\n" + "\n".join(f"• {d}" for d in details[:8]))
    if comments and not done:
        # What they said on their Showcase, which home, and a link that opens that exact home on
        # the agent's Showcases page (signed in).
        parts.append(f"\U0001F4AC THEIR SHOWCASE COMMENT{'S' if len(comments) > 1 else ''}\n" + "\n\n".join(
            f'“{c["text"][:400]}”\n\U0001F3E0 {c["home"]}\n\U0001F449 Open it in your Showcases:\n{c["url"]}' for c in comments[:3]))
    if your_notes and task.auto_source != NOTED_GUEST_SOURCE:  # that alert already quotes the note
        parts.append("\U0001F4DD YOUR NOTES\n" + "\n".join(
            f"• {'(' + label + ') ' if label else ''}{text[:280]}" for label, text in your_notes[:3]))
    msg = None if done else (message_override if message_override is not None else full_message(task, showcase, owner))
    if msg:
        # Steps before sending, on their own lines apart from the text to copy (instructions, not
        # part of it): book it in ShowingTime, then fill in the time.
        block = [_DIVIDER]
        if task.auto_source in SHOWING_RULES:
            # The listing info, boxed in by a divider above and below. Calendar notes are plain text,
            # so no bold / italics - and "bold" look-alike Unicode letters would paste into
            # ShowingTime as different characters and the search would find nothing.
            p = prop or {}
            block.append("⚠️ BOOK IN SHOWINGTIME FIRST")
            if p.get("mls"):
                block.append(f"\U0001F50E Search MLS ID: {p['mls']}")
            else:
                block.append(f"\U0001F50E Search the address: {p.get('short') or 'the home'}")
            la = p.get("list_agent") or {}
            if la.get("name"):
                block.append(f"\U0001F3F7️ Listing Agent: {la['name']}")
            if la.get("phone"):
                block.append(f"\U0001F4DE Listing Agent's Number: {la['phone']}")
            block += [_DIVIDER, ""]  # a line of space before the next section
        if FILL_IN_TIME in msg:
            block += [f"✏️ BEFORE YOU SEND: replace {FILL_IN_TIME} with the showing you booked.", ""]
        if FILL_IN_ANSWER in msg:
            block += [f"✏️ BEFORE YOU SEND: replace {FILL_IN_ANSWER} with your answer to their message (it's above).", ""]
        if done_link and getattr(lead, "phone", None):
            # One tap: Messages opens with their number and this text written, from the agent's own
            # cell - and the follow-up checks itself off.
            block += [f"\U0001F4F2 Text {first} from your phone - opens Messages with the text below already written:", f"{done_link}/text", ""]
        block += ["\U0001F4AC SUGGESTED TEXT - copy everything below ⬇️", "", msg]
        parts.append("\n".join(block))
    body: dict = {
        "summary": summary,
        "description": "\n\n".join(parts) or None,
        # Google shows this as a named link on the event (web) - the same one-tap done page.
        "source": {"title": ("Undo - OpenHousePal" if done else "Mark as done - OpenHousePal"), "url": done_link} if done_link
        else {"title": "OpenHousePal", "url": "https://openhousepal.com/leads/tasks"},
        "extendedProperties": {"private": {"ohp_kind": "task", "ohp_id": task.id}},
        # The agent's chosen colour (Follow-ups page) - Graphite once done, so it recedes. Per-event,
        # so it works under our narrow Google scope. (Apple Calendar and Outlook ignore per-event
        # colours and paint every event in the calendar's own colour.)
        "colorId": "8" if done else color,
        # Shown as FREE: a follow-up reminder must never block the agent's availability.
        "transparency": "transparent",
    }
    done_day = task.completed_at.astimezone(EASTERN).date() if done else None
    if done and task.all_day:
        # Crossed out on the day it was checked off when that's later than planned (it had been
        # carried forward onto "today"), otherwise on its own day.
        d = max(task_day(task), done_day)
        body["start"] = {"date": d.isoformat()}
        body["end"] = {"date": (d + timedelta(days=1)).isoformat()}
    elif done:
        start = due
        if due.astimezone(EASTERN).date() < done_day:
            clock = due.astimezone(EASTERN).timetz()
            start = datetime.combine(done_day, clock.replace(tzinfo=None), tzinfo=EASTERN)
        body["start"] = _local(start)
        body["end"] = _local(start + timedelta(minutes=15 if task.auto_source else 30))
        body["reminders"] = {"useDefault": False, "overrides": []}  # no alert for a finished one
    elif carried_forward(task):
        # Missed automatic follow-up: re-synced each morning onto TODAY. A timed one keeps its
        # clock time (so it still alerts); an all-day one stays all-day.
        today = datetime.now(EASTERN).date()
        if task.all_day:
            body["start"] = {"date": today.isoformat()}
            body["end"] = {"date": (today + timedelta(days=1)).isoformat()}
        else:
            clock = due.astimezone(EASTERN).timetz()
            start = datetime.combine(today, clock.replace(tzinfo=None), tzinfo=EASTERN)
            body["start"] = _local(start)
            body["end"] = _local(start + timedelta(minutes=15 if task.auto_source else 30))
            body["reminders"] = {"useDefault": False, "overrides": reminder_overrides(owner)}
    elif task.all_day:
        # The Eastern day, not the UTC one - an evening task would otherwise land a day late.
        d = task_day(task)
        body["start"] = {"date": d.isoformat()}
        body["end"] = {"date": (d + timedelta(days=1)).isoformat()}
    else:
        # Automatic follow-ups are a 15-minute "reach out now" slot; hand-made tasks 30 minutes.
        # Both alert the way the agent chose on the Follow-ups page (default: 15 min before + at start).
        length = 15 if task.auto_source else 30
        body["start"] = _local(due)
        body["end"] = _local(due + timedelta(minutes=length))
        body["reminders"] = {"useDefault": False, "overrides": reminder_overrides(owner)}
    return {k: v for k, v in body.items() if v is not None}


def _oh_event_body(oh: OpenHouseEvent) -> dict:
    d = oh.scheduled_date
    st = oh.scheduled_start_time or time(12, 0)
    et = oh.scheduled_end_time or time(14, 0)
    start = datetime.combine(d, st, tzinfo=EASTERN).astimezone(timezone.utc)
    end = datetime.combine(d, et, tzinfo=EASTERN).astimezone(timezone.utc)
    # oh.address is sometimes just the street line and sometimes already the full address - only
    # append city/state when they're not already in it, so we don't get "..., Springfield, IL".
    parts = [oh.address] if oh.address else []
    low = (oh.address or "").lower()
    for extra in (oh.city, oh.state):
        if extra and extra.lower() not in low:
            parts.append(extra)
    loc = ", ".join(parts)
    return {
        "summary": f"[Open House] {oh.abbreviated_address or oh.address or 'Listing'}",
        "location": loc or None,
        "start": _local(start),
        "end": _local(end),
        "source": {"title": "OpenHousePal", "url": "https://openhousepal.com/open-houses"},
        "extendedProperties": {"private": {"ohp_kind": "open_house", "ohp_id": oh.id}},
    }


def _tour_window(tour: PropertyTour) -> tuple[datetime, datetime]:
    # confirmed_date is "YYYY-MM-DD", confirmed_time is 24h "HH:MM" - Eastern wall-clock time,
    # same convention _oh_event_body uses. A tour is booked as one hour.
    y, mo, d = (int(x) for x in tour.confirmed_date.split("-"))
    hh, mm = (int(x) for x in tour.confirmed_time.split(":"))
    start = datetime(y, mo, d, hh, mm, tzinfo=EASTERN).astimezone(timezone.utc)
    return start, start + timedelta(hours=1)


def _tour_event_body(tour: PropertyTour, prop: Optional[Property]) -> dict:
    start, end = _tour_window(tour)
    # Once the hour is over (or it's marked done) the event stays as a record - grey, silent,
    # "✅ DONE" - the same look a checked-off follow-up gets.
    done = bool(tour.is_completed) or end <= datetime.now(timezone.utc)
    addr = None
    if prop:
        addr = prop.full_address or ", ".join(p for p in (prop.street_address, prop.city, prop.state) if p)
    from app.services.tasks_service import short_person
    raw_street = (prop.street_address if prop else None) or (addr or "").split(",")[0]
    street = (raw_street or "").strip()
    street = street.title() if street.isupper() else street  # MLS sends ALL CAPS
    desc = [f"Tour requested by {tour.visitor_name}"]
    if tour.visitor_email:
        desc.append(tour.visitor_email)
    if tour.visitor_phone:
        desc.append(tour.visitor_phone)
    if tour.message:
        desc.append(f"\nVisitor note: {tour.message}")
    title = f"[Tour] {street or 'Property'} - {short_person(tour.visitor_name) or 'Visitor'}"
    return {
        # Short title - "[Tour] 732 Buck Ln - Jane B."; the location + notes carry the rest.
        "summary": f"{DONE_PREFIX}{title}" if done else _bold(title),
        **({"colorId": "8"} if done else {}),
        "location": addr or None,
        "description": "\n".join(desc),
        "start": _local(start),
        "end": _local(end),
        "source": {"title": "OpenHousePal", "url": "https://openhousepal.com/showcases"},
        "extendedProperties": {"private": {"ohp_kind": "tour", "ohp_id": tour.id}},
        "reminders": {"useDefault": False, "overrides": [] if done else [{"method": "popup", "minutes": 60}]},
    }


async def _showing_event_body(db: AsyncSession, sub) -> Optional[dict]:
    """An Agent Hub "Schedule a Showing" the agent confirmed - same look as a Showcase tour."""
    info = sub.showing or {}
    if not info.get("confirmed_date") or not info.get("confirmed_time"):
        return None
    try:
        y, mo, d = (int(x) for x in str(info["confirmed_date"]).split("-"))
        hh, mm = (int(x) for x in str(info["confirmed_time"]).split(":"))
    except ValueError:
        return None
    start = datetime(y, mo, d, hh, mm, tzinfo=EASTERN).astimezone(timezone.utc)
    end = start + timedelta(hours=1)
    done = end <= datetime.now(timezone.utc)
    from app.services.tasks_service import short_person
    prop = None
    key = info.get("listing_key")
    if key:
        prop = (await db.execute(select(Property).where(Property.listing_key == key).limit(1))).scalars().first()
    addr = (prop.full_address if prop else None) or sub.property_address or None
    raw_street = (prop.street_address if prop else None) or (addr or "").split(",")[0]
    street = (raw_street or "").strip()
    street = street.title() if street.isupper() else street
    desc = [f"Showing requested by {sub.contact_name or 'a visitor'}"]
    if sub.contact_email:
        desc.append(sub.contact_email)
    if sub.contact_phone:
        desc.append(sub.contact_phone)
    if info.get("note"):
        desc.append(f"\nVisitor note: {info['note']}")
    title = f"[Showing] {street or 'Property'} - {short_person(sub.contact_name) or 'Visitor'}"
    return {
        "summary": f"{DONE_PREFIX}{title}" if done else _bold(title),
        **({"colorId": "8"} if done else {}),
        "location": addr,
        "description": "\n".join(desc),
        "start": _local(start),
        "end": _local(end),
        "source": {"title": "OpenHousePal", "url": "https://openhousepal.com/leads"},
        "extendedProperties": {"private": {"ohp_kind": "showing", "ohp_id": sub.id}},
        "reminders": {"useDefault": False, "overrides": [] if done else [{"method": "popup", "minutes": 60}]},
    }


async def _sync_showing(db: AsyncSession, conn: CalendarConnection, submission_id: str) -> None:
    from app.models.database import FormSubmission
    sub = (await db.execute(select(FormSubmission).where(FormSubmission.id == submission_id))).scalar_one_or_none()
    if not sub or sub.owner_id != conn.owner_id:
        return
    body = await _showing_event_body(db, sub)
    if body is None:
        await _remove_event(db, conn, "showing", submission_id)
        return
    await _upsert_event(db, conn, "showing", submission_id, body)


def sync_showing_bg(owner_id: str, submission_id: str) -> None:
    async def run():
        for provider in PROVIDERS:
            await _with_conn(owner_id, provider, lambda db, conn: _sync_showing(db, conn, submission_id))
    _spawn(run())


def _host_open_event_body(req: OpenHouseHostRequest, prop: Optional[Property], listing_agent: Optional[User],
                          listing_notes: Optional[str], open_note: Optional[str]) -> dict:
    """An open house another agent's listing approved you to host: the home's real address as the
    location (tap it for directions - nothing to type), and in the notes the listing agent's
    standing host notes for the property plus anything said about this particular open."""
    st = req.requested_start_time or time(12, 0)
    et = req.requested_end_time or time(14, 0)
    start = datetime.combine(req.requested_date, st, tzinfo=EASTERN).astimezone(timezone.utc)
    end = datetime.combine(req.requested_date, et, tzinfo=EASTERN).astimezone(timezone.utc)
    street = (prop.street_address if prop else None) or "the listing"
    nice_street = street.title() if street.isupper() else street
    loc = None
    if prop:
        tc = lambda v: v.title() if v and v.isupper() else v
        # Street + town in normal case (MLS sends ALL CAPS); state + zip as-is - maps reads it fine.
        loc = ", ".join(p for p in (tc(prop.street_address), tc(prop.city), f"{prop.state or ''} {prop.zipcode or ''}".strip()) if p) or prop.full_address
    desc = []
    if listing_agent:
        who = " ".join(x for x in (listing_agent.first_name, listing_agent.last_name) if x)
        bits = [b for b in (listing_agent.brokerage, getattr(listing_agent, "phone", None), listing_agent.email) if b]
        desc.append(f"Hosting for {who}" + (f" ({' | '.join(bits)})" if bits else ""))
    if listing_notes and listing_notes.strip():
        desc.append(f"\nNotes from the listing agent:\n{listing_notes.strip()}")
    if open_note and open_note.strip():
        desc.append(f"\nFor this open:\n{open_note.strip()}")
    base = os.getenv("FRONTEND_URL", os.getenv("CLIENT_URL", "https://openhousepal.com")).rstrip("/")
    desc.append(f"\nYour kit: {base}/open-houses?build={req.id}")
    return {
        "summary": f"[Open House] {nice_street}",
        "location": loc or None,
        "description": "\n".join(desc),
        "start": _local(start),
        "end": _local(end),
        "source": {"title": "OpenHousePal", "url": f"{base}/open-houses"},
        "extendedProperties": {"private": {"ohp_kind": "host_open", "ohp_id": req.id}},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 60}, {"method": "popup", "minutes": 24 * 60}]},
    }


async def host_open_body_for(db: AsyncSession, req: OpenHouseHostRequest) -> dict:
    prop = (await db.execute(select(Property).where(Property.listing_key == req.listing_key))).scalars().first()
    listing_agent = await db.get(User, req.listing_owner_agent_id)
    note = (await db.execute(select(OpenHouseListingNote).where(OpenHouseListingNote.listing_key == req.listing_key))).scalar_one_or_none()
    # Something said about THIS open: the listing agent's note on the time they countered with,
    # or the message on the team broadcast it was claimed from.
    open_note = req.counter_message
    if not open_note and req.broadcast_id:
        bc = await db.get(OpenHouseTeamBroadcast, req.broadcast_id)
        open_note = bc.message if bc else None
    return _host_open_event_body(req, prop, listing_agent, note.notes if note else None, open_note)


async def _link(db: AsyncSession, conn_id: str, kind: str, source_id: str) -> Optional[CalendarEventLink]:
    return (await db.execute(
        select(CalendarEventLink).where(
            CalendarEventLink.connection_id == conn_id,
            CalendarEventLink.kind == kind,
            CalendarEventLink.source_id == source_id,
        )
    )).scalar_one_or_none()


# One in-flight sync per (connection, kind, source) inside this process. Two syncs of the same
# task racing (e.g. the auto-created follow-up's background sync + an immediate edit) used to
# BOTH insert an event -> a duplicate on the agent's calendar. Across processes the unique
# constraint + the IntegrityError path below still catch it.
_UPSERT_LOCKS: dict[tuple, asyncio.Lock] = {}


async def _upsert_event(db: AsyncSession, conn: CalendarConnection, kind: str, source_id: str, body: dict) -> None:
    key = (conn.id, kind, source_id)
    lock = _UPSERT_LOCKS.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            await _upsert_event_locked(db, conn, kind, source_id, body)
    finally:
        if not lock.locked():
            _UPSERT_LOCKS.pop(key, None)


async def _upsert_event_locked(db: AsyncSession, conn: CalendarConnection, kind: str, source_id: str, body: dict) -> None:
    client = _client_for(conn)
    token = await _valid_access_token(db, conn)
    # Plain values up front: a rollback below expires `conn`, and touching its attributes after
    # that raised inside the cleanup's try/except - so the duplicate event was never deleted.
    conn_id, calendar_id = conn.id, conn.calendar_id
    digest = _body_hash(body)
    link = await _link(db, conn_id, kind, source_id)
    if link:
        if link.body_hash == digest:
            return  # already exactly this on their calendar - "Sync now" doesn't touch it again
        try:
            await client.patch_event(token, calendar_id, link.external_event_id, body)
        except Exception as e:  # noqa: BLE001
            gone = any(c in str(e) for c in ("404", "410"))
            if not (gone and kind == "task"):
                raise
            # The agent deleted this follow-up's event themselves = done (same as
            # reconcile_deleted_followups) - never put it back.
            task = await db.get(AgentTask, source_id)
            if task is not None and task.completed_at is None:
                task.completed_at = datetime.now(timezone.utc)
                from app.services.tasks_service import resolve_request_for_followup
                await resolve_request_for_followup(db, task)
            await db.delete(link)
            await db.commit()
            return
        link.updated_at = datetime.now(timezone.utc)
        link.body_hash = digest
        db.add(link)
        await db.commit()
        return

    event_id = await client.insert_event(token, calendar_id, body)
    db.add(CalendarEventLink(connection_id=conn_id, kind=kind, source_id=source_id, external_event_id=event_id, body_hash=digest))
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent sync of the same task/open house won the insert race. Roll back, drop the
        # orphan event we just made, and patch the surviving one so the latest body still wins.
        await db.rollback()
        try:
            await client.delete_event(token, calendar_id, event_id)
        except Exception:
            logger.warning("could not delete duplicate calendar event %s", event_id, exc_info=True)
        existing = await _link(db, conn_id, kind, source_id)
        if existing:
            await client.patch_event(token, calendar_id, existing.external_event_id, body)
            existing.updated_at = datetime.now(timezone.utc)
            db.add(existing)
            await db.commit()


def _body_hash(body: dict) -> str:
    import hashlib
    import json as _json
    return hashlib.sha1(_json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()


async def _remove_event(db: AsyncSession, conn: CalendarConnection, kind: str, source_id: str) -> None:
    link = await _link(db, conn.id, kind, source_id)
    if not link:
        return
    client = _client_for(conn)
    token = await _valid_access_token(db, conn)
    await client.delete_event(token, conn.calendar_id, link.external_event_id)
    await db.delete(link)
    await db.commit()


# --------------------------------------------------------------------------- public: OAuth lifecycle

async def complete_oauth(db: AsyncSession, owner_id: str, code: str, provider: str = "google") -> CalendarConnection:
    """Finish the OAuth dance: exchange the code, store tokens, create the OHP calendar, and kick
    off a background back-fill. Raises on a hard failure (the callback route turns that into a
    ?calendar=error redirect)."""
    client = PROVIDERS[provider]
    tok = await client.exchange_code(code)
    info = await client.get_userinfo(tok["access_token"])

    conn = await get_connection(db, owner_id, provider)
    if not conn:
        conn = CalendarConnection(owner_id=owner_id, provider=provider)
        db.add(conn)
    conn.access_token = tok["access_token"]
    if tok.get("refresh_token"):
        conn.refresh_token = tok["refresh_token"]
    conn.token_expiry = tok["expiry"]
    conn.scopes = tok.get("scope") or " ".join(client.SCOPES)
    conn.account_email = info.get("email")
    conn.last_error = None

    # (Re)create the dedicated calendar if we don't have one / it's gone.
    if not conn.calendar_id or not await client.get_calendar(conn.access_token, conn.calendar_id):
        conn.calendar_id = await client.create_app_calendar(conn.access_token)
    # Shown + OpenHousePal gold, on every (re)connect. Best-effort - never blocks connecting.
    await client.style_app_calendar(conn.access_token, conn.calendar_id)
    await client.set_calendar_timezone(conn.access_token, conn.calendar_id, CALENDAR_TZ)
    await db.commit()
    await db.refresh(conn)

    _spawn(_backfill_bg(owner_id, provider))
    return conn


async def disconnect(db: AsyncSession, owner_id: str, provider: str = "google") -> None:
    conn = await get_connection(db, owner_id, provider)
    if not conn:
        return
    client = PROVIDERS[provider]
    links = (await db.execute(
        select(CalendarEventLink).where(CalendarEventLink.connection_id == conn.id)
    )).scalars().all()
    # Best-effort: delete the events we made + the calendar, then revoke.
    try:
        token = await _valid_access_token(db, conn)
        for link in links:
            try:
                await client.delete_event(token, conn.calendar_id, link.external_event_id)
            except Exception:
                pass
        if conn.calendar_id:
            try:
                await client.delete_calendar(token, conn.calendar_id)
            except Exception:
                pass
        if conn.refresh_token:
            await client.revoke(conn.refresh_token)
    except Exception:
        logger.warning("calendar disconnect cleanup partial", exc_info=True)
    await db.delete(conn)   # event_links cascade
    await db.commit()


# --------------------------------------------------------------------------- public: sync entrypoints
# The *_bg wrappers open their own session so a slow provider call never blocks task/OH CRUD, and
# loop over every provider so an agent with both Google and Microsoft connected gets both updated.

async def _with_conn(owner_id: str, provider: str, fn) -> None:
    from app.database import AsyncSessionLocal
    client = PROVIDERS[provider]
    if not client.is_configured():
        return
    async with AsyncSessionLocal() as db:
        conn = await get_connection(db, owner_id, provider)
        if not conn or not conn.calendar_id:
            return
        try:
            await fn(db, conn)
            await _note_ok(db, conn)
        except Exception as e:  # noqa: BLE001
            logger.warning("calendar sync failed for %s/%s: %s", owner_id, provider, e)
            try:
                await _note_error(db, conn, str(e))
            except Exception:
                pass


def sync_task_bg(owner_id: str, task_id: str) -> None:
    async def run():
        for provider in PROVIDERS:
            await _with_conn(owner_id, provider, lambda db, conn: _sync_task(db, conn, task_id))
    _spawn(run())


def unsync_task_bg(owner_id: str, task_id: str) -> None:
    async def run():
        for provider in PROVIDERS:
            await _with_conn(owner_id, provider, lambda db, conn: _remove_event(db, conn, "task", task_id))
    _spawn(run())


def sync_open_house_bg(owner_id: str, oh_id: str) -> None:
    async def run():
        for provider in PROVIDERS:
            await _with_conn(owner_id, provider, lambda db, conn: _sync_open_house(db, conn, oh_id))
    _spawn(run())


def unsync_open_house_bg(owner_id: str, oh_id: str) -> None:
    async def run():
        for provider in PROVIDERS:
            await _with_conn(owner_id, provider, lambda db, conn: _remove_event(db, conn, "open_house", oh_id))
    _spawn(run())


def sync_host_open_bg(owner_id: str, request_id: str) -> None:
    """An approved open house you're hosting for another agent -> your calendar (and back off if it
    stops being approved)."""
    async def run():
        for provider in PROVIDERS:
            await _with_conn(owner_id, provider, lambda db, conn: _sync_host_open(db, conn, request_id))
    _spawn(run())


def unsync_host_open_bg(owner_id: str, request_id: str) -> None:
    async def run():
        for provider in PROVIDERS:
            await _with_conn(owner_id, provider, lambda db, conn: _remove_event(db, conn, "host_open", request_id))
    _spawn(run())


def sync_tour_bg(owner_id: str, tour_id: str) -> None:
    async def run():
        for provider in PROVIDERS:
            await _with_conn(owner_id, provider, lambda db, conn: _sync_tour(db, conn, tour_id))
    _spawn(run())


def unsync_tour_bg(owner_id: str, tour_id: str) -> None:
    async def run():
        for provider in PROVIDERS:
            await _with_conn(owner_id, provider, lambda db, conn: _remove_event(db, conn, "tour", tour_id))
    _spawn(run())


async def sync_tour_now(owner_id: str, tour_id: str, timeout: float = 12.0) -> str:
    """Foreground calendar push used the moment an agent confirms a tour, so the UI can say what
    actually happened. Returns 'added' | 'not_connected' | 'error'. Never raises. Pushes to every
    connected provider; reports success if any of them took it."""
    from app.database import AsyncSessionLocal
    any_connected = False
    any_ok = False
    for provider, client in PROVIDERS.items():
        if not client.is_configured():
            continue
        try:
            async with AsyncSessionLocal() as db:
                conn = await get_connection(db, owner_id, provider)
                if not conn or not conn.calendar_id:
                    continue
                any_connected = True
                try:
                    await asyncio.wait_for(_sync_tour(db, conn, tour_id), timeout=timeout)
                    await _note_ok(db, conn)
                    any_ok = True
                except Exception as e:  # noqa: BLE001
                    logger.warning("inline tour calendar sync failed for %s/%s: %s", owner_id, provider, e)
                    try:
                        await _note_error(db, conn, str(e))
                    except Exception:
                        pass
        except Exception:  # noqa: BLE001
            continue
    if any_ok:
        return "added"
    return "error" if any_connected else "not_connected"


async def _sync_task(db: AsyncSession, conn: CalendarConnection, task_id: str) -> None:
    task = (await db.execute(select(AgentTask).where(AgentTask.id == task_id))).scalar_one_or_none()
    if not task or task.owner_id != conn.owner_id:
        return
    # A task with no date can't sit on the calendar. A completed one stays, crossed out
    # (_task_event_body); deleting a task in the app is what takes its event off. Except a done
    # request follow-up (send the disclosure, book the showing...): once it's handled there's
    # nothing left to see, so it comes off the calendar (user, 10/15).
    from app.services.tasks_service import ACTION_REQUEST_RULES, SIGNIN_FOLLOWUP_SOURCES, open_house_followups_mode
    if task.due_at is None or (task.completed_at is not None and task.auto_source in ACTION_REQUEST_RULES):
        await _remove_event(db, conn, "task", task_id)
        return
    # The open house these came from has "keep follow-ups off my calendar" on: they stay on the
    # Follow-ups page only.
    if task.auto_source in SIGNIN_FOLLOWUP_SOURCES and await open_house_followups_mode(db, task.auto_source_id) != "on":
        await _remove_event(db, conn, "task", task_id)
        return
    await _upsert_event(db, conn, "task", task_id, await task_body_for(db, task))


async def _showcase_url_for(db: AsyncSession, task: AgentTask) -> Optional[str]:
    info = await _showcase_info_for(db, task)
    return info["url"] if info else None


async def _showcase_info_for(db: AsyncSession, task: AgentTask) -> Optional[dict]:
    """Unrepresented lead with a live Showcase -> {url, count, pitch} for the event."""
    if not task.lead_id:
        return None
    try:
        from app.models.database import Lead, User
        from app.services.tasks_service import lead_showcase_info
        owner = await db.get(User, task.owner_id)
        lead = await db.get(Lead, task.lead_id)
        if owner is None or lead is None:
            return None
        return (await lead_showcase_info(db, owner, [lead])).get(lead.id)
    except Exception:
        logger.warning("showcase lookup failed for task %s", task.id, exc_info=True)
        return None


async def task_body_for(db: AsyncSession, task: AgentTask) -> dict:
    """Everything a follow-up's calendar event needs: the agent's colour, the Showcase pitch and
    the one-tap done link (minted on first sync for tasks made before done links existed)."""
    from app.models.database import Lead, User
    from app.services.tasks_service import (contact_url, done_url, ensure_done_code, followup_color, followup_property,
                                            lead_comment_details, lead_profile, request_details)
    owner = await db.get(User, task.owner_id)
    code = await ensure_done_code(db, task)
    lead = await db.get(Lead, task.lead_id) if task.lead_id else None
    your_notes = None
    if lead is not None:
        try:
            your_notes = (await lead_profile(db, lead))["notes"]
        except Exception:
            logger.warning("lead notes lookup failed for task %s", task.id, exc_info=True)
    comments = None
    try:
        comments = (await lead_comment_details(db, task.owner_id, [task])).get(task.id)
    except Exception:
        logger.warning("showcase comments lookup failed for task %s", task.id, exc_info=True)
    details = None
    try:
        details = await request_details(db, task)
    except Exception:
        logger.warning("request details lookup failed for task %s", task.id, exc_info=True)
    prop = None
    try:
        prop = await followup_property(db, task, owner)
    except Exception:
        logger.warning("follow-up property lookup failed for task %s", task.id, exc_info=True)
    return _task_event_body(task, await _showcase_info_for(db, task), followup_color(owner), done_url(code),
                            owner=owner, contact_link=contact_url(code) if lead is not None else None,
                            your_notes=your_notes, lead_name=(lead.name or "").split(" ")[0] if lead is not None else None,
                            comments=comments, lead=lead, details=details, prop=prop)


async def _sync_open_house(db: AsyncSession, conn: CalendarConnection, oh_id: str) -> None:
    oh = (await db.execute(select(OpenHouseEvent).where(OpenHouseEvent.id == oh_id))).scalar_one_or_none()
    if not oh or oh.agent_id != conn.owner_id:
        return
    if oh.is_deleted or not oh.scheduled_date:
        await _remove_event(db, conn, "open_house", oh_id)
        return
    await _upsert_event(db, conn, "open_house", oh_id, _oh_event_body(oh))


async def _sync_host_open(db: AsyncSession, conn: CalendarConnection, request_id: str) -> None:
    req = await db.get(OpenHouseHostRequest, request_id)
    if not req or req.requesting_agent_id != conn.owner_id:
        return
    if req.status != "approved":
        await _remove_event(db, conn, "host_open", request_id)
        return
    await _upsert_event(db, conn, "host_open", request_id, await host_open_body_for(db, req))


async def _sync_tour(db: AsyncSession, conn: CalendarConnection, tour_id: str) -> None:
    tour = (await db.execute(select(PropertyTour).where(PropertyTour.id == tour_id))).scalar_one_or_none()
    if not tour:
        return
    coll = (await db.execute(select(Collection).where(Collection.id == tour.collection_id))).scalar_one_or_none()
    if not coll or coll.owner_id != conn.owner_id:
        return
    # Only a confirmed tour belongs on the calendar - unconfirming pulls the event back off. A
    # completed (or past) one stays, crossed out as DONE (see _tour_event_body).
    if not tour.confirmed_date or not tour.confirmed_time:
        await _remove_event(db, conn, "tour", tour_id)
        return
    prop = (await db.execute(select(Property).where(Property.id == tour.property_id))).scalar_one_or_none()
    await _upsert_event(db, conn, "tour", tour_id, _tour_event_body(tour, prop))


# --------------------------------------------------------------------------- back-fill + busy read

async def _backfill_bg(owner_id: str, provider: str = "google") -> None:
    await _with_conn(owner_id, provider, _backfill)


async def _backfill(db: AsyncSession, conn: CalendarConnection) -> None:
    await _client_for(conn).set_calendar_timezone(await _valid_access_token(db, conn), conn.calendar_id, CALENDAR_TZ)
    # Follow-ups the agent deleted off the calendar count as done FIRST, so this sync can't put
    # them back; then only events that actually changed are sent (_upsert_event's body hash).
    try:
        await reconcile_deleted_followups(db, conn.owner_id)
    except Exception:  # noqa: BLE001
        logger.warning("calendar delete check before sync failed", exc_info=True)
    now = datetime.now(timezone.utc)
    horizon_past = now - timedelta(days=30)

    tasks = (await db.execute(
        select(AgentTask).where(
            AgentTask.owner_id == conn.owner_id,
            AgentTask.completed_at.is_(None),
            AgentTask.due_at.is_not(None),
            AgentTask.due_at >= horizon_past,
        ).limit(500)
    )).scalars().all()
    for t in tasks:
        try:
            await _sync_task(db, conn, t.id)
        except Exception:  # noqa: BLE001 - one bad event must not stop the rest of the sync
            logger.warning("calendar sync of one item failed", exc_info=True)
            await db.rollback()

    opens = (await db.execute(
        select(OpenHouseEvent).where(
            OpenHouseEvent.agent_id == conn.owner_id,
            OpenHouseEvent.is_deleted.is_(False),
            OpenHouseEvent.scheduled_date.is_not(None),
            OpenHouseEvent.scheduled_date >= (now.date() - timedelta(days=7)),
        ).limit(300)
    )).scalars().all()
    for oh in opens:
        try:
            await _upsert_event(db, conn, "open_house", oh.id, _oh_event_body(oh))
        except Exception:  # noqa: BLE001 - one bad event must not stop the rest of the sync
            logger.warning("calendar sync of one item failed", exc_info=True)
            await db.rollback()

    hosting = (await db.execute(
        select(OpenHouseHostRequest).where(
            OpenHouseHostRequest.requesting_agent_id == conn.owner_id,
            OpenHouseHostRequest.status == "approved",
            OpenHouseHostRequest.requested_date >= now.date(),
        ).limit(200)
    )).scalars().all()
    for req in hosting:
        try:
            await _upsert_event(db, conn, "host_open", req.id, await host_open_body_for(db, req))
        except Exception:  # noqa: BLE001 - one bad event must not stop the rest of the sync
            logger.warning("calendar sync of one item failed", exc_info=True)
            await db.rollback()


async def trigger_backfill(owner_id: str, provider: str = "google") -> None:
    _spawn(_backfill_bg(owner_id, provider))


# --------------------------------------------------------------------------- calendar -> app

# Deleting a follow-up off the calendar is the agent saying "done" - the same as checking it off in
# Follow-ups (the count drops, it moves to Done). Each open, dated follow-up we put on a connected
# calendar is looked up; one that's gone (or cancelled) is marked complete and its link dropped, so
# no later sync puts it back on that calendar. Another connected calendar's copy is updated like a
# normal check-off. Runs every few minutes (main.py) and, throttled, when Follow-ups is opened.
_RECONCILE_EVERY = timedelta(seconds=60)
_last_reconcile: dict[str, datetime] = {}


async def reconcile_deleted_followups(db: AsyncSession, owner_id: str) -> int:
    done = 0
    now = datetime.now(timezone.utc)
    for conn in await get_connections(db, owner_id):
        client = PROVIDERS.get(conn.provider)
        if not client or not client.is_configured() or not conn.calendar_id:
            continue
        rows = (await db.execute(
            select(CalendarEventLink.id, CalendarEventLink.external_event_id, AgentTask.id)
            .join(AgentTask, AgentTask.id == CalendarEventLink.source_id)
            .where(
                CalendarEventLink.connection_id == conn.id,
                CalendarEventLink.kind == "task",
                AgentTask.owner_id == owner_id,
                AgentTask.completed_at.is_(None),
                AgentTask.due_at.is_not(None),
                AgentTask.due_at > now - timedelta(days=60),
            )
        )).all()
        if not rows:
            continue
        conn_id, calendar_id, provider = conn.id, conn.calendar_id, conn.provider
        try:
            token = await _valid_access_token(db, conn)
        except Exception:  # noqa: BLE001 - a broken connection shows on the Calendar card instead
            continue
        for link_id, event_id, task_id in rows:
            if await client.event_exists(token, calendar_id, event_id) is not False:
                continue
            task = await db.get(AgentTask, task_id)
            link = await db.get(CalendarEventLink, link_id)
            if not task or task.completed_at is not None:
                continue
            task.completed_at = datetime.now(timezone.utc)
            from app.services.tasks_service import resolve_request_for_followup
            await resolve_request_for_followup(db, task)  # a disclosure follow-up = disclosure sent
            if link:
                await db.delete(link)
            await db.commit()
            done += 1
            logger.info("follow-up %s marked done - its event was deleted from the %s calendar", task_id, provider)
            for other in await get_connections(db, owner_id):
                if other.id != conn_id and other.calendar_id:
                    try:
                        await _sync_task(db, other, task_id)
                    except Exception:  # noqa: BLE001
                        logger.warning("could not update follow-up %s on the %s calendar", task_id, other.provider, exc_info=True)
    return done


async def reconcile_deleted_followups_throttled(owner_id: str, timeout: float = 4.0) -> None:
    """For the Follow-ups page: at most once a minute per agent, never slower than `timeout`."""
    now = datetime.now(timezone.utc)
    last = _last_reconcile.get(owner_id)
    if last and now - last < _RECONCILE_EVERY:
        return
    _last_reconcile[owner_id] = now
    from app.database import AsyncSessionLocal

    async def run():
        async with AsyncSessionLocal() as db:
            await reconcile_deleted_followups(db, owner_id)
    try:
        await asyncio.wait_for(asyncio.shield(_track(run())), timeout)
    except asyncio.TimeoutError:
        pass  # keeps going in the background; the next refresh shows it
    except Exception:  # noqa: BLE001
        logger.warning("calendar delete check failed for %s", owner_id, exc_info=True)


def _track(coro):
    t = asyncio.ensure_future(coro)
    _PENDING.add(t)
    t.add_done_callback(_PENDING.discard)
    return t


async def scheduled_reconcile_deleted_followups() -> None:
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        owner_ids = list((await db.execute(
            select(CalendarConnection.owner_id).where(CalendarConnection.calendar_id.is_not(None)).distinct()
        )).scalars().all())
    for owner_id in owner_ids:
        try:
            async with AsyncSessionLocal() as db:
                await reconcile_deleted_followups(db, owner_id)
            _last_reconcile[owner_id] = datetime.now(timezone.utc)
        except Exception:  # noqa: BLE001
            logger.warning("calendar delete check failed for %s", owner_id, exc_info=True)


# Events re-titled before this moment may still carry the old "✅ DONE ·" / crossed-out title.
_RETITLE_BEFORE = datetime(2026, 10, 23, tzinfo=timezone.utc)


async def retitle_done_events() -> None:
    """Once, shortly after a deploy: follow-ups still to do get their bold title, and follow-ups and
    tours checked off in the last 3 weeks get their calendar title re-written to the plain "DONE ·" one (the old emoji / cross-out title showed
    bigger than the open ones on the iPhone). One event at a time; each is touched once."""
    from app.database import AsyncSessionLocal
    now = datetime.now(timezone.utc)
    if now >= _RETITLE_BEFORE + timedelta(days=7):
        return
    async with AsyncSessionLocal() as db:
        tasks = (await db.execute(
            select(CalendarConnection.owner_id, CalendarConnection.provider, AgentTask.id)
            .join(CalendarEventLink, and_(CalendarEventLink.kind == "task", CalendarEventLink.source_id == AgentTask.id))
            .join(CalendarConnection, CalendarConnection.id == CalendarEventLink.connection_id)
            .where(or_(AgentTask.completed_at >= now - timedelta(days=21),
                       and_(AgentTask.completed_at.is_(None), AgentTask.due_at >= now - timedelta(days=14))),
                   CalendarEventLink.updated_at < _RETITLE_BEFORE)
        )).all()
        tours = (await db.execute(
            select(CalendarConnection.owner_id, CalendarConnection.provider, PropertyTour.id)
            .join(CalendarEventLink, and_(CalendarEventLink.kind == "tour", CalendarEventLink.source_id == PropertyTour.id))
            .join(CalendarConnection, CalendarConnection.id == CalendarEventLink.connection_id)
            .where(PropertyTour.confirmed_date.is_not(None), PropertyTour.confirmed_date >= (now - timedelta(days=21)).date().isoformat(),
                   CalendarEventLink.updated_at < _RETITLE_BEFORE)
        )).all()
    for owner_id, provider, tid in tasks:
        try:
            await _with_conn(owner_id, provider, lambda db, conn, t=tid: _sync_task(db, conn, t))
        except Exception:  # noqa: BLE001
            logger.warning("re-titling done follow-up %s failed", tid, exc_info=True)
    for owner_id, provider, tid in tours:
        try:
            await _with_conn(owner_id, provider, lambda db, conn, t=tid: _sync_tour(db, conn, t))
        except Exception:  # noqa: BLE001
            logger.warning("re-titling tour %s failed", tid, exc_info=True)
    logger.info("re-titled %d done follow-up and %d tour events", len(tasks), len(tours))


async def scheduled_finish_past_tours() -> None:
    """Every 15 minutes: confirmed tours whose hour is over get their calendar event crossed out
    ("✅ DONE", grey, no alert). Only events not already re-written since the tour ended, and only
    tours from the last few days, so each one is touched once."""
    from app.database import AsyncSessionLocal
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(PropertyTour, CalendarEventLink, CalendarConnection)
            .join(CalendarEventLink, and_(CalendarEventLink.kind == "tour", CalendarEventLink.source_id == PropertyTour.id))
            .join(CalendarConnection, CalendarConnection.id == CalendarEventLink.connection_id)
            .where(PropertyTour.confirmed_date.is_not(None), PropertyTour.confirmed_time.is_not(None),
                   PropertyTour.confirmed_date >= (now - timedelta(days=4)).date().isoformat())
        )).all()
        due = []
        for tour, link, conn in rows:
            try:
                _, end = _tour_window(tour)
            except (ValueError, AttributeError):
                continue
            if end <= now and _to_utc(link.updated_at) < end:
                due.append((conn.owner_id, conn.provider, tour.id))
    for owner_id, provider, tour_id in due:
        try:
            await _with_conn(owner_id, provider, lambda db, conn, tid=tour_id: _sync_tour(db, conn, tid))
        except Exception:  # noqa: BLE001
            logger.warning("crossing out past tour %s failed", tour_id, exc_info=True)


async def get_busy(db: AsyncSession, owner_id: str, start: datetime, end: datetime) -> list[dict]:
    """Busy blocks across every connected calendar (Google + Microsoft), merged. [] if none
    connected."""
    out: list[dict] = []
    for provider, client in PROVIDERS.items():
        if not client.is_configured():
            continue
        conn = await get_connection(db, owner_id, provider)
        if not conn:
            continue
        try:
            token = await _valid_access_token(db, conn)
            out.extend(await client.free_busy(token, start, end))
        except Exception as e:  # noqa: BLE001
            logger.warning("free/busy read failed for %s/%s: %s", owner_id, provider, e)
    return out


# Public "when is the agent busy" for a showing request: only start/end times (never titles or
# details), the next `days` days, cached a minute per agent so opening the window repeatedly
# doesn't hit Google / Microsoft each time.
_public_busy_cache: dict[str, tuple[datetime, list[dict]]] = {}


async def public_busy(db: AsyncSession, owner_id: str, days: int = 8) -> list[dict]:
    now = datetime.now(timezone.utc)
    days = max(1, min(int(days or 8), 14))
    key = owner_id if days == 8 else f"{owner_id}:{days}"
    hit = _public_busy_cache.get(key)
    if hit and (now - hit[0]).total_seconds() < 60:
        return hit[1]
    start = now.replace(minute=0, second=0, microsecond=0)
    try:
        blocks = await get_busy(db, owner_id, start, start + timedelta(days=days + 1))
    except Exception:  # noqa: BLE001
        logger.warning("public busy read failed for %s", owner_id, exc_info=True)
        blocks = []
    def _hours(b: dict) -> float:
        try:
            return (datetime.fromisoformat(str(b["end"]).replace("Z", "+00:00")) - datetime.fromisoformat(str(b["start"]).replace("Z", "+00:00"))).total_seconds() / 3600
        except Exception:  # noqa: BLE001
            return 0
    # All-day items (birthdays, reminders, "out of office" banners) would cross out a whole day -
    # only timed events count as a conflict.
    out = [{"start": b.get("start"), "end": b.get("end")} for b in blocks if b.get("start") and b.get("end") and _hours(b) < 23]
    _public_busy_cache[key] = (now, out)
    return out


def status_dict(conn: Optional[CalendarConnection]) -> Optional[dict]:
    if not conn:
        return None
    # Surface "you need to reconnect" when the provider has clearly stopped accepting our tokens
    # (the agent revoked OpenHousePal from their account, a security event invalidated the grant,
    # or the refresh token is missing) - otherwise the calendar just quietly stops updating.
    needs_reconnect = _needs_reconnect(conn, conn.last_error)
    return {
        "connected": True,
        "provider": conn.provider,
        "account_email": conn.account_email,
        "calendar_name": "OpenHousePal",
        "last_sync_at": conn.last_sync_at.isoformat() if conn.last_sync_at else None,
        "last_error": conn.last_error,
        "needs_reconnect": needs_reconnect,
    }
