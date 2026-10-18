"""A real calendar invite for the CLIENT, attached to their "your tour is confirmed" email (user,
10/18 - "calendar invites for clients"). Gmail, Apple Mail / iPhone and Outlook show it as an event
with Yes / No buttons and "Add to calendar", whether or not the agent connected a calendar.

It's a separate invite written for the client ("Tour of 732 Buck Ln with Peter Stefanatos"), not
the agent's own calendar event: that one is titled for the agent ("[Tour] 732 Buck Ln - Jane B.")
and changes when it's marked done, which would email the client every time.

The UID is fixed per tour, so when the agent changes the time the client's calendar MOVES the
event instead of adding a second one (SEQUENCE always goes up).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


def _esc(v: str) -> str:
    return (v or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545: lines longer than 75 octets continue on the next line after a space."""
    out, raw = [], line.encode("utf-8")
    while len(raw) > 73:
        cut = 73
        while cut > 0 and (raw[cut] & 0xC0) == 0x80:   # never split a UTF-8 character
            cut -= 1
        out.append(raw[:cut].decode("utf-8"))
        raw = raw[cut:]
    out.append(raw.decode("utf-8"))
    return "\r\n ".join(out)


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def eastern_start(date_str: Optional[str], time_str: Optional[str]) -> Optional[datetime]:
    """'YYYY-MM-DD' + 24h 'HH:MM' Eastern wall-clock (how tours / showings are stored) -> aware dt."""
    try:
        y, mo, d = (int(x) for x in str(date_str).split("-"))
        hh, mm = (int(x) for x in str(time_str).split(":"))
        return datetime(y, mo, d, hh, mm, tzinfo=EASTERN)
    except (TypeError, ValueError):
        return None


def build_invite(*, uid: str, start: datetime, minutes: int = 60, street: str, address: Optional[str],
                 agent_name: str, agent_email: Optional[str], agent_phone: Optional[str],
                 client_name: Optional[str], client_email: str, link: Optional[str] = None) -> str:
    """The .ics text (METHOD:REQUEST) for one confirmed tour."""
    end = start + timedelta(minutes=minutes)
    now = datetime.now(timezone.utc)
    who = agent_name or "your agent"
    desc = [f"Your tour of {street} with {who}."]
    reach = " · ".join(x for x in (agent_phone, agent_email) if x)
    if reach:
        desc.append(f"Questions or running late? {reach}")
    if link:
        desc.append(link)
    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//OpenHousePal//Tour invite//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{uid}@openhousepal.com",
        f"DTSTAMP:{_utc(now)}",
        f"SEQUENCE:{int(now.timestamp())}",
        f"DTSTART:{_utc(start)}",
        f"DTEND:{_utc(end)}",
        f"SUMMARY:{_esc(f'Tour of {street} with {who}')}",
        f"LOCATION:{_esc(address or street)}",
        f"DESCRIPTION:{_esc(chr(10).join(desc))}",
        "STATUS:CONFIRMED",
        "TRANSP:OPAQUE",
    ]
    if agent_email:
        lines.append(f"ORGANIZER;CN={_esc(who)}:mailto:{agent_email}")
    lines.append(f"ATTENDEE;CN={_esc(client_name or client_email)};ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{client_email}")
    if link:
        lines.append(f"URL:{link}")
    lines += [
        "BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_esc(f'Tour of {street} in 1 hour')}", "TRIGGER:-PT1H", "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"
