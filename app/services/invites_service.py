"""Invites (Forms & Invites -> Invites).

An agent makes a designed invitation - an open house, a broker's open, a client event / dinner or
anything custom - and shares one short link (/i/<code>, on openhousepal.com or on their own IDX
domain when they have one connected). Guests open it (an envelope animation), RSVP yes / maybe /
no with the agent's questions, and can come back to change their answer.

What happens on an RSVP:
- the RSVP row (event_invite_rsvps) - the agent's guest list;
- client invites (audience 'clients'): the guest becomes / is attached to a Lead with the
  'invite_rsvp' source (leads_service.upsert_lead, so CRM push etc. work like any other lead);
  agent invites (broker's opens) never make Leads;
- the agent gets the bell + their "New leads" email (template new_lead_captured - the prod Mailgun
  domain is at its template cap, so no new Mailgun template is needed for this feature);
- the guest gets a short personal confirmation note (+ a calendar invite when they're coming),
  rendered in code by personal_email_service like the other personal notes, from the agent's own
  mailbox when they connected one.

Plan access: every active plan can make invites (like Forms); the public page stops when the
owner's subscription has ended (owner_has_access). Links move to the agent's own domain only when
site_service says so (IDX + connected own domain) - the same rule as every other agent link.
"""
from __future__ import annotations

import re
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import (
    AgentHub, EventInvite, EventInviteRecipient, EventInviteRsvp, Notification, StudioBrandKit, User,
)

logger = get_logger(__name__)

EASTERN = ZoneInfo("America/New_York")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_ACCENT = "#C9A24D"
CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o/1/l/i - easy to read aloud or type
MAX_RECIPIENTS_PER_INVITE = 500
RESEND_GAP = timedelta(hours=12)


# ------------------------------------------------------------------------------------- presets

def _q(qid: str, qtype: str, label: str, options: Optional[list[str]] = None, *, required: bool = False,
       role: Optional[str] = None, placeholder: Optional[str] = None, helper_text: Optional[str] = None) -> dict:
    q: dict[str, Any] = {"id": qid, "type": qtype, "label": label, "required": required}
    if options:
        q["options"] = options
    if role:
        q["role"] = role
    if placeholder:
        q["placeholder"] = placeholder
    if helper_text:
        q["helper_text"] = helper_text
    return q


PARTY = ["Just me", "2", "3", "4", "5+"]

PRESETS: dict[str, dict] = {
    "open_house": {
        "label": "Open House",
        "blurb": "Invite buyers, neighbors and clients to tour the home.",
        "audience": "clients",
        "design": "classic",
        "title": "You're invited to an Open House",
        "message": "Come see this beautiful home in person - stop by any time during the open house. I'd love to show you around.",
        "questions": [
            # "Are you a real estate agent?" / "Are you working with one?" are asked by the RSVP
            # itself now (10/24) for every client invite - no question needed for it here.
            _q("party", "select", "How many of you are coming?", PARTY, required=True, role="guests"),
            _q("plans", "select", "Are you looking to…", ["Buy", "Sell", "Buy and sell", "Just curious"]),
            _q("notes", "long_text", "Anything you'd like to see or ask about?", placeholder="The kitchen, the schools, the HOA…"),
        ],
    },
    "brokers_open": {
        "label": "Broker's Open",
        "blurb": "Invite agents to preview the listing before the public.",
        "audience": "agents",
        "design": "midnight",
        "title": "Broker's Open - you're invited",
        "message": "Join me for a first look at my new listing. Bring your questions and your buyers in mind - lunch is on me.",
        "questions": [
            _q("brokerage", "short_text", "Your brokerage", required=True, role="brokerage", placeholder="Brokerage name"),
            _q("party", "select", "How many from your office?", ["Just me", "2", "3", "4+"], required=True, role="guests"),
            _q("buyers", "select", "Do you have buyers who might be a fit?", ["Yes", "Maybe", "Not right now"]),
            _q("lunch", "select", "Will you stay for lunch?", ["Yes", "No thanks"]),
        ],
    },
    "client_event": {
        "label": "Client Event / Dinner",
        "blurb": "A dinner, a party or an appreciation night for your clients.",
        "audience": "clients",
        "design": "garden",
        "title": "Join us for dinner",
        "message": "A small thank-you to the people who make this work so rewarding. Good food, good company - I hope you can make it.",
        "questions": [
            _q("party", "select", "How many in your party (you included)?", PARTY, required=True, role="guests"),
            _q("guests", "short_text", "Names of your guests", placeholder="So we can welcome them by name"),
            _q("diet", "multi_select", "Any dietary needs?", ["None", "Vegetarian", "Vegan", "Gluten-free", "Dairy-free", "Nut allergy", "Other"]),
            _q("notes", "long_text", "Anything else we should know?"),
        ],
    },
    "custom": {
        "label": "Custom",
        "blurb": "Anything else - a market talk, a happy hour, a home-buying class.",
        "audience": "clients",
        "design": "modern",
        "title": "You're invited",
        "message": "",
        "questions": [
            _q("party", "select", "How many of you are coming?", PARTY, required=True, role="guests"),
            _q("notes", "long_text", "Anything you'd like us to know?"),
        ],
    },
}

DESIGNS = ("classic", "garden", "midnight", "modern")
MOTIONS = ("normal", "luxe")
METALS = {"gold": "#C9A24D", "silver": "#A9AFB7", "bronze": "#A9714B"}  # lib/luxeMetals base tones
QUESTION_TYPES = {"short_text", "long_text", "select", "multi_select", "number", "phone", "email", "date"}
MAX_DAYS = 7


def presets_out() -> list[dict]:
    return [{"id": k, **{x: v[x] for x in ("label", "blurb", "audience", "design", "title", "message", "questions")}}
            for k, v in PRESETS.items()]


# ------------------------------------------------------------------------------------- helpers

def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(7))


async def unique_code(db: AsyncSession) -> str:
    for _ in range(12):
        code = new_code()
        if not (await db.execute(select(EventInvite.id).where(EventInvite.code == code))).first():
            return code
    return new_code() + secrets.choice(CODE_ALPHABET)


def clean_questions(questions: Iterable[Any]) -> list[dict]:
    """Same shape as the forms' questions; ids made unique, options trimmed, max 12."""
    out: list[dict] = []
    seen: set[str] = set()
    for raw in list(questions or [])[:12]:
        q = raw.model_dump(exclude_none=True) if hasattr(raw, "model_dump") else dict(raw or {})
        qtype = q.get("type")
        label = str(q.get("label") or "").strip()[:200]
        if qtype not in QUESTION_TYPES or not label:
            continue
        qid = re.sub(r"[^a-zA-Z0-9_-]", "", str(q.get("id") or ""))[:64] or uuid.uuid4().hex[:10]
        while qid in seen:
            qid = f"{qid}_{uuid.uuid4().hex[:4]}"
        seen.add(qid)
        item: dict[str, Any] = {"id": qid, "type": qtype, "label": label, "required": bool(q.get("required"))}
        if qtype in ("select", "multi_select"):
            opts = [str(o).strip()[:120] for o in (q.get("options") or []) if str(o).strip()][:20]
            if not opts:
                continue
            item["options"] = opts
        for k in ("placeholder", "helper_text"):
            if q.get(k):
                item[k] = str(q[k]).strip()[:300]
        if q.get("role") in ("guests", "represented", "brokerage"):
            item["role"] = q["role"]
        out.append(item)
    return out


def clean_accent(value: Optional[str]) -> Optional[str]:
    v = (value or "").strip()
    return v.upper() if _HEX_RE.match(v) else None


def _lum(hexv: str) -> float:
    n = int(hexv[1:], 16)
    return (0.2126 * ((n >> 16) & 255) + 0.7152 * ((n >> 8) & 255) + 0.0722 * (n & 255)) / 255


async def brand_defaults(db: AsyncSession, user: User) -> dict:
    """The agent's own colour for a new invitation: their Brand Kit's Classic colour (secondary,
    the one safe on buttons), else the Agent Hub's open-house theme colour, else the house gold."""
    accent = None
    kit = (await db.execute(select(StudioBrandKit).where(StudioBrandKit.owner_id == user.id))).scalar_one_or_none()
    if kit is not None:
        c = clean_accent(kit.secondary_color)
        if c and _lum(c) <= 0.85:
            accent = c
    if accent is None:
        hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == user.id))).scalar_one_or_none()
        theme = ((hub.config or {}).get("openHouseRequestTheme") or {}) if hub is not None else {}
        c = clean_accent(theme.get("accentColor"))
        if c and _lum(c) <= 0.85:
            accent = c
    return {"accent_color": accent or DEFAULT_ACCENT, "logo_url": getattr(kit, "personal_logo_url", None) if kit else None}


async def share_base(db: AsyncSession, user: User) -> str:
    """openhousepal.com for everyone; the agent's own connected domain when they have one."""
    try:
        from app.services.website.site_service import resolve_owner_public_base_url
        return (await resolve_owner_public_base_url(db, user)).rstrip("/")
    except Exception:  # noqa: BLE001 - a link must always come back
        from app.services.agent_email_service import app_url
        return app_url()


def platform_url(code: str) -> str:
    from app.services.agent_email_service import app_url
    return f"{app_url()}/i/{code}"


def clock(v: Optional[str]) -> str:
    if not v:
        return ""
    h, m = (int(x) for x in v.split(":"))
    return f"{h % 12 or 12}{f':{m:02d}' if m else ''} {'PM' if h >= 12 else 'AM'}"


def clock_range(a: Optional[str], b: Optional[str]) -> str:
    if not a:
        return ""
    if not b:
        return clock(a)
    x, y = clock(a), clock(b)
    return f"{x[:-3]} - {y}" if x[-2:] == y[-2:] else f"{x} - {y}"


def _as_date(v: Any) -> Optional[date]:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def all_days(inv: EventInvite) -> list[dict]:
    """Every day of the invite, soonest first: [{date: date, start, end}] - the first is always
    event_date / start_time / end_time (10/24: several days)."""
    out: list[dict] = []
    if inv.event_date:
        out.append({"date": inv.event_date, "start": inv.start_time, "end": inv.end_time})
    for raw in inv.extra_dates or []:
        d = _as_date((raw or {}).get("date"))
        if d and not any(x["date"] == d for x in out):
            out.append({"date": d, "start": (raw or {}).get("start"), "end": (raw or {}).get("end")})
    return sorted(out, key=lambda x: x["date"])[:MAX_DAYS]


def day_label(day: dict, *, short: bool = False) -> str:
    """"Saturday, November 8 · 1 - 3 PM" (short: "Sat, Nov 8 · 1 - 3 PM")."""
    d = day["date"]
    name = f"{d.strftime('%a, %b')} {d.day}" if short else f"{d.strftime('%A, %B')} {d.day}"
    rng = clock_range(day.get("start"), day.get("end"))
    return f"{name} · {rng}" if rng else name


def days_label(days: list[dict]) -> str:
    """One day -> its long label. Several with the same hours -> "Sat, Nov 8 & Sun, Nov 9 · 1 - 3 PM";
    different hours -> each short label joined with " / "."""
    if not days:
        return ""
    if len(days) == 1:
        return day_label(days[0])
    same = len({(x.get("start"), x.get("end")) for x in days}) == 1
    if same:
        names = [f"{x['date'].strftime('%a, %b')} {x['date'].day}" for x in days]
        joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " & " + names[-1]
        rng = clock_range(days[0].get("start"), days[0].get("end"))
        return f"{joined} · {rng}" if rng else joined
    return " / ".join(day_label(x, short=True) for x in days)


def when_label(inv: EventInvite) -> str:
    """"Saturday, November 8 · 1 - 3 PM" - or every day of a several-day invite."""
    return days_label(all_days(inv))


def is_past(inv: EventInvite) -> bool:
    """Past once its LAST day has gone by."""
    days = all_days(inv)
    if not days:
        return False
    return days[-1]["date"] < datetime.now(EASTERN).date()


def upcoming_days(inv: EventInvite) -> list[dict]:
    today = datetime.now(EASTERN).date()
    return [d for d in all_days(inv) if d["date"] >= today]


_AUTO_TITLE = re.compile(r"^\s*(open\s+house|broker['’]?s\s+open)\s+at\s+", re.I)


def display_title(inv: EventInvite) -> str:
    """The title where a label already says "Open House" / "Broker's Open" next to it (owner, 10/24
    round 3 - not "OPEN HOUSE · Open House at ..."): the automatic title shows just the street. The
    frontend twin is lib/inviteTitle.ts cardTitle."""
    t = (inv.title or "").strip()
    if inv.invite_type in ("open_house", "brokers_open") and _AUTO_TITLE.match(t):
        from app.services.agent_roundup_service import nice_street
        rest = _AUTO_TITLE.sub("", t).strip()
        return nice_street(rest) or t
    return t


def line_two(inv: EventInvite) -> str:
    """The flyer's line 2 when the agent typed their own (round 8) - shown under the title in the
    invite / RSVP emails too. '' when it's the automatic line (emails already say where)."""
    return (inv.subtitle or "").strip()


def event_phrase(inv: EventInvite) -> str:
    """How an email names the event in a sentence: "the open house at 239 Windham Dr"."""
    from app.services.agent_roundup_service import nice_street
    street = nice_street(((inv.linked_property or {}).get("address") or inv.address or "").split(",")[0])
    if inv.invite_type == "open_house":
        return f"the open house at {street}" if street else "the open house"
    if inv.invite_type == "brokers_open":
        return f"the broker's open at {street}" if street else "the broker's open"
    return inv.title


def place_label(inv: EventInvite) -> str:
    return ", ".join(x for x in (inv.place_name, inv.address) if x) or ""


def _start_end(day: dict) -> tuple[Optional[datetime], Optional[datetime]]:
    d = day.get("date") if day else None
    if not d:
        return None, None
    try:
        hh, mm = (int(x) for x in (day.get("start") or "18:00").split(":"))
        start = datetime(d.year, d.month, d.day, hh, mm, tzinfo=EASTERN)
        if day.get("end"):
            eh, em = (int(x) for x in day["end"].split(":"))
            end = datetime(d.year, d.month, d.day, eh, em, tzinfo=EASTERN)
            if end <= start:
                end = start + timedelta(hours=2)
        else:
            end = start + timedelta(hours=2)
        return start, end
    except (TypeError, ValueError):
        return None, None


def ics_text(inv: EventInvite, *, agent_name: str, url: str, method: str = "PUBLISH", only: Optional[Iterable[date]] = None) -> Optional[str]:
    """An .ics for "Add to calendar" (PUBLISH) or attached to the confirmation email (REQUEST) -
    one event per day (`only` = just the days this guest is coming to)."""
    from app.services.calendar_invite_service import _esc, _fold, _utc
    wanted = set(only or [])
    days = [d for d in all_days(inv) if not wanted or d["date"] in wanted] or all_days(inv)
    desc = " ".join(x for x in ((inv.message or "").strip(), f"Hosted by {agent_name}." if agent_name else "", url) if x)
    events: list[str] = []
    for i, day in enumerate(days):
        start, end = _start_end(day)
        if not start or not end:
            continue
        events += [
            "BEGIN:VEVENT",
            f"UID:invite-{inv.id}{'' if i == 0 else '-' + day['date'].isoformat()}@openhousepal.com",
            f"DTSTAMP:{_utc(datetime.now(timezone.utc))}",
            f"DTSTART:{_utc(start)}",
            f"DTEND:{_utc(end)}",
            f"SUMMARY:{_esc(inv.title)}",
            f"DESCRIPTION:{_esc(desc)}",
            f"LOCATION:{_esc(place_label(inv))}",
            f"URL:{url}",
            "SEQUENCE:0", "STATUS:CONFIRMED", "TRANSP:OPAQUE",
            "END:VEVENT",
        ]
    if not events:
        return None
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//OpenHousePal//Invites//EN", "CALSCALE:GREGORIAN", f"METHOD:{method}",
             *events, "END:VCALENDAR"]
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"


def guest_count_of(questions: list[dict], answers: dict[str, Any], response: str) -> int:
    if response == "no":
        return 0
    for q in questions:
        if q.get("role") == "guests":
            raw = str(answers.get(q["id"]) or "").strip().lower()
            if not raw:
                return 1
            if raw.startswith("just"):
                return 1
            digits = re.findall(r"\d+", raw)
            return max(1, min(50, int(digits[0]))) if digits else 1
    return 1


def represented_of(questions: list[dict], answers: dict[str, Any], has_agent: Optional[bool] = None) -> Optional[str]:
    if has_agent is not None:
        return "yes" if has_agent else "no"
    for q in questions:
        if q.get("role") == "represented":
            v = str(answers.get(q["id"]) or "").strip().lower()
            if v.startswith("yes"):
                return "yes"
            if v.startswith("no"):
                return "no"
    return None


def lead_kind_of(inv: EventInvite, rsvp: EventInviteRsvp) -> str:
    """Who this person is to the agent (user, 10/24):
    - 'agent'       a real estate agent (or any RSVP to an agents' invite) - never a lead;
    - 'represented' not an agent, but working with one - not a lead (the agent still hears they're coming);
    - 'declined'    "can't make it" and not interested in the home - nothing to follow up on;
    - 'lead'        not an agent and no agent - a real lead (follow-up, the dashboard's Leads)."""
    if inv.audience == "agents" or rsvp.is_agent:
        return "agent"
    answers = {a.get("questionId"): a.get("value") for a in (rsvp.answers or [])}
    if represented_of(inv.questions or [], answers, rsvp.has_agent) == "yes":
        return "represented"
    if rsvp.response == "no" and not rsvp.still_interested:
        return "declined"
    return "lead"


def _answer_text(v: Any) -> str:
    if isinstance(v, list):
        return ", ".join(str(x) for x in v if str(x).strip())
    return str(v if v is not None else "").strip()


def answers_snapshot(questions: list[dict], raw: Iterable[Any]) -> tuple[list[dict], dict[str, Any]]:
    """Validated answers in the forms' snapshot shape [{questionId, label, type, value}] + a map."""
    by_id = {q["id"]: q for q in questions}
    given: dict[str, Any] = {}
    for a in raw or []:
        qid = getattr(a, "questionId", None) if not isinstance(a, dict) else a.get("questionId")
        val = getattr(a, "value", None) if not isinstance(a, dict) else a.get("value")
        q = by_id.get(qid or "")
        if not q:
            continue
        if q["type"] == "multi_select":
            vals = val if isinstance(val, list) else [val] if val else []
            vals = [str(v)[:120] for v in vals if str(v) in (q.get("options") or [])]
            if vals:
                given[q["id"]] = vals
        elif q["type"] == "select":
            if val is not None and str(val) in (q.get("options") or []):
                given[q["id"]] = str(val)
        else:
            s = str(val if val is not None else "").strip()[:2000]
            if s:
                given[q["id"]] = s
    snap = [{"questionId": q["id"], "label": q["label"], "type": q["type"], "value": given[q["id"]]}
            for q in questions if q["id"] in given]
    return snap, given


# ------------------------------------------------------------------------------------- counts / serialize

async def counts_for(db: AsyncSession, invite_ids: list[str]) -> dict[str, dict]:
    if not invite_ids:
        return {}
    rows = (await db.execute(
        select(
            EventInviteRsvp.invite_id,
            func.sum(case((EventInviteRsvp.response == "yes", 1), else_=0)),
            func.sum(case((EventInviteRsvp.response == "maybe", 1), else_=0)),
            func.sum(case((EventInviteRsvp.response == "no", 1), else_=0)),
            func.sum(case((EventInviteRsvp.response == "yes", EventInviteRsvp.guest_count), else_=0)),
            func.sum(case((EventInviteRsvp.response == "maybe", EventInviteRsvp.guest_count), else_=0)),
            func.sum(case((EventInviteRsvp.is_seen.is_(False), 1), else_=0)),
            func.max(EventInviteRsvp.created_at),
        ).where(EventInviteRsvp.invite_id.in_(invite_ids)).group_by(EventInviteRsvp.invite_id)
    )).all()
    out = {i: {"yes": 0, "maybe": 0, "no": 0, "guests_yes": 0, "guests_maybe": 0, "unseen": 0, "last_rsvp_at": None} for i in invite_ids}
    for r in rows:
        out[r[0]] = {"yes": int(r[1] or 0), "maybe": int(r[2] or 0), "no": int(r[3] or 0),
                     "guests_yes": int(r[4] or 0), "guests_maybe": int(r[5] or 0), "unseen": int(r[6] or 0),
                     "last_rsvp_at": r[7].isoformat() if r[7] else None}
    return out


def invite_out(inv: EventInvite, *, share_url: str, counts: Optional[dict] = None) -> dict:
    return {
        "id": inv.id, "code": inv.code, "invite_type": inv.invite_type, "audience": inv.audience,
        "title": inv.title, "subtitle": inv.subtitle, "message": inv.message,
        "event_date": inv.event_date.isoformat() if inv.event_date else None,
        "start_time": inv.start_time, "end_time": inv.end_time,
        "place_name": inv.place_name, "address": inv.address,
        "linked_property": inv.linked_property, "cover_image_url": inv.cover_image_url,
        "design": inv.design, "accent_color": inv.accent_color or DEFAULT_ACCENT,
        "questions": inv.questions or [], "status": inv.status, "view_count": inv.view_count or 0,
        "when_label": when_label(inv), "is_past": is_past(inv),
        "dates": [{"date": d["date"].isoformat(), "start": d.get("start"), "end": d.get("end")} for d in all_days(inv)],
        "extra_dates": [{"date": d["date"].isoformat(), "start": d.get("start"), "end": d.get("end")} for d in all_days(inv)[1:]],
        "motion": inv.motion or "normal", "luxe_metal": inv.luxe_metal,
        "cover_position": inv.cover_position, "open_house_event_id": inv.open_house_event_id,
        "share_url": share_url, "platform_url": platform_url(inv.code),
        "created_at": inv.created_at.isoformat() if inv.created_at else None,
        "updated_at": inv.updated_at.isoformat() if inv.updated_at else None,
        "counts": counts or {"yes": 0, "maybe": 0, "no": 0, "guests_yes": 0, "guests_maybe": 0, "unseen": 0, "last_rsvp_at": None},
    }


def rsvp_out(r: EventInviteRsvp, *, private: bool = True) -> dict:
    out = {
        "id": r.id, "response": r.response, "name": r.name, "guest_count": r.guest_count,
        "answers": r.answers or [],
        "days": list(r.days or []), "is_agent": r.is_agent, "has_agent": r.has_agent,
        "still_interested": r.still_interested, "keep_updated": r.keep_updated,
        "requests": {k: True for k in (r.requests or {})},
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }
    if private:
        out.update({"email": r.email, "phone": r.phone, "lead_id": r.lead_id, "is_seen": r.is_seen, "lead_kind": r.lead_kind,
                    "requests": r.requests or {}})
    return out


def apply_save(inv: EventInvite, payload, accent_default: str) -> None:
    inv.invite_type = payload.invite_type
    inv.audience = payload.audience or PRESETS[payload.invite_type]["audience"]
    if payload.invite_type == "brokers_open":
        inv.audience = "agents"
    inv.title = payload.title.strip()[:140]
    # line 2 exactly as typed (round 8): None = automatic town line, '' = the agent removed it
    sub = getattr(payload, "subtitle", None)
    inv.subtitle = None if sub is None else sub.strip()[:160]
    inv.message = (payload.message or "").strip()[:2000] or None
    # Every day, soonest first: the first is event_date / start_time / end_time, the rest extra_dates.
    days: list[dict] = []
    if payload.event_date:
        days.append({"date": payload.event_date, "start": payload.start_time, "end": payload.end_time if payload.start_time else None})
    for x in getattr(payload, "extra_dates", None) or []:
        if not any(d["date"] == x.date for d in days):
            days.append({"date": x.date, "start": x.start, "end": x.end if x.start else None})
    days = sorted(days, key=lambda d: d["date"])[:MAX_DAYS]
    first = days[0] if days else {"date": None, "start": None, "end": None}
    inv.event_date = first["date"]
    inv.start_time = first["start"]
    inv.end_time = first["end"]
    inv.extra_dates = [{"date": d["date"].isoformat(), "start": d["start"], "end": d["end"]} for d in days[1:]] or None
    inv.motion = payload.motion if getattr(payload, "motion", None) in MOTIONS else "normal"
    inv.luxe_metal = getattr(payload, "luxe_metal", None) if getattr(payload, "luxe_metal", None) in METALS else None
    pos = getattr(payload, "cover_position", None)
    inv.cover_position = {"x": round(pos.x, 1), "y": round(pos.y, 1)} if pos is not None else None
    inv.place_name = (payload.place_name or "").strip()[:140] or None
    inv.address = (payload.address or "").strip()[:300] or None
    lp = payload.linked_property.model_dump(exclude_none=True) if payload.linked_property else None
    if lp and isinstance(lp.get("photos"), list):
        lp["photos"] = [str(p) for p in lp["photos"] if p][:12]
    inv.linked_property = lp or None
    cover = (payload.cover_image_url or "").strip()
    inv.cover_image_url = cover if cover and (cover.startswith("http") or cover.startswith("/uploads/")) else None
    inv.design = payload.design if payload.design in DESIGNS else "classic"
    inv.accent_color = METALS[inv.luxe_metal].upper() if inv.luxe_metal else (clean_accent(payload.accent_color) or accent_default)
    inv.questions = clean_questions(payload.questions)


# ------------------------------------------------------------------------------------- RSVP side effects

def rsvp_days(inv: EventInvite, rsvp: EventInviteRsvp) -> list[dict]:
    """The days this guest is coming to (all of them when they didn't pick / there's only one)."""
    chosen = {d for d in (_as_date(x) for x in (rsvp.days or [])) if d}
    days = all_days(inv)
    picked = [d for d in days if d["date"] in chosen]
    return picked or days


def builtin_answers(rsvp: EventInviteRsvp) -> list[dict]:
    """The RSVP's own two questions, in the forms' answer shape (for the lead's activity + follow-up)."""
    out = []
    if rsvp.is_agent is not None:
        out.append({"questionId": "is_agent", "label": "Are you a real estate agent?", "type": "select", "value": "Yes" if rsvp.is_agent else "No"})
    if rsvp.has_agent is not None:
        out.append({"questionId": "has_agent", "label": "Are you working with an agent?", "type": "select", "value": "Yes" if rsvp.has_agent else "No"})
    return out


def invite_link(inv: EventInvite) -> str:
    return f"/studio/forms?view=invites&invite={inv.id}"


async def notify_agent(db: AsyncSession, owner: User, inv: EventInvite, rsvp: EventInviteRsvp, *, changed: bool, lead_id: Optional[str]) -> None:
    """The bell (+ the Activity page) and the agent's designed RSVP email (invite_email_service).
    Everyone who answers shows up here - agents, people with an agent and "can't make it" too;
    only real leads reach the dashboard's Leads (attach_lead). Never raises."""
    from app.services.agent_email_service import absolute, app_url, email_agent
    kind = rsvp.lead_kind or lead_kind_of(inv, rsvp)
    word = {"yes": "is coming", "maybe": "might come", "no": "can't make it"}[rsvp.response]
    if rsvp.response == "no" and rsvp.still_interested:
        word = "can't make it - still interested in the home"
    name = rsvp.name or "A guest"
    extra = f" (+{rsvp.guest_count - 1})" if rsvp.response != "no" and rsvp.guest_count > 1 else ""
    who = {"agent": " (agent)", "represented": " (has an agent)"}.get(kind, "")
    title = f"{'Updated RSVP' if changed else 'New RSVP'}: {name}{extra}{who} {word}"[:200]
    link = invite_link(inv)
    days = rsvp_days(inv, rsvp) if rsvp.response != "no" else []
    several = len(all_days(inv)) > 1
    try:
        db.add(Notification(
            agent_id=owner.id, type="INVITE_RSVP", reference_type="INVITE_RSVP", reference_id=rsvp.id,
            title=title, message=(f"{name} {word} - {inv.title}" + (f" ({days_label(days)})" if several and days else ""))[:500],
            visitor_name=name,
            property_address=(inv.linked_property or {}).get("address") or inv.address,
            link=link, is_read=False, created_at=datetime.now(timezone.utc),
        ))
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("invite rsvp bell failed", exc_info=True)
        await db.rollback()
    try:
        totals = (await counts_for(db, [inv.id])).get(inv.id) or {}
    except Exception:  # noqa: BLE001
        totals = {}
    email_agent(owner, "INVITE_RSVP", "invite_rsvp_agent", title, {
        "guest_name": name, "rsvp_response": rsvp.response, "guest_count": rsvp.guest_count,
        "lead_kind": kind, "still_interested": bool(rsvp.still_interested), "keep_updated": bool(rsvp.keep_updated),
        "invite_title": display_title(inv), "invite_subtitle": line_two(inv), "invite_when": when_label(inv), "invite_kind": PRESETS.get(inv.invite_type, {}).get("label") or "Invite",
        "days_label": days_label(days) if several and days else "",
        "guest_email": rsvp.email or "", "guest_phone": rsvp.phone or "",
        "answers": [a for a in (rsvp.answers or [])], "changed": changed,
        "totals": {"coming": totals.get("guests_yes", 0), "maybe": totals.get("maybe", 0), "no": totals.get("no", 0)},
        "link": absolute(link), "lead_link": absolute(f"/leads/{lead_id}") if lead_id else "", "app_url": app_url(),
    })


async def existing_lead(db: AsyncSession, owner_id: str, *, email: Optional[str], phone: Optional[str], name: Optional[str]):
    """The agent's lead for this person, if they already have one (user, 10/24: "attach it to the
    existing lead page - no duplicate"): the same email, or - when the RSVP gave no matching email -
    the same phone number AND the same first name (a shared household / office phone alone is not
    the same person). Never creates."""
    from app.models.database import Lead
    from app.services.leads_service import find_lead_by_contact, normalize_phone
    lead = await find_lead_by_contact(db, owner_id, email, None)
    if lead is not None or not phone:
        return lead
    digits = (normalize_phone(phone) or "")[-10:]
    first = (name or "").strip().split(" ")[0].lower()
    if len(digits) < 10 or not first:
        return None
    rows = (await db.execute(select(Lead).where(Lead.owner_id == owner_id, Lead.phone.like(f"%{digits}"))
                             .order_by(Lead.created_at.asc()).limit(20))).scalars().all()
    for row in rows:
        if (row.name or "").strip().split(" ")[0].lower() != first:
            continue
        if email and row.email and row.email.lower() != email.lower():
            continue  # two different email addresses - not provably the same person
        return row
    return None


async def attach_lead(db: AsyncSession, owner: User, inv: EventInvite, rsvp: EventInviteRsvp, *, changed: bool) -> Optional[str]:
    """Sorts the guest (lead_kind_of) and puts the RSVP on the right lead:
    - a real lead (no agent, not an agent): becomes / joins a Lead ('invite_rsvp' source) - the
      normal lead treatment: a follow-up, the dashboard's Leads, CRM push;
    - agents, people with an agent and "can't make it, not interested": no new lead and no
      follow-up - they're on the Invites guest list + the bell/Activity only. If the agent ALREADY
      has them as a lead, the RSVP is added to that lead's page (no duplicate, no follow-up)."""
    from app.services.leads_service import log_activity, upsert_lead
    kind = lead_kind_of(inv, rsvp)
    if rsvp.lead_kind != kind:
        rsvp.lead_kind = kind
        db.add(rsvp)
        await db.commit()
    if not (rsvp.email or rsvp.phone):
        return rsvp.lead_id
    word = {"yes": "Coming", "maybe": "Maybe", "no": "Can't make it"}[rsvp.response]
    if rsvp.response == "no" and rsvp.still_interested:
        word = "Can't make it - still interested in the home"
    summary = (f"Changed RSVP to {word} - {inv.title}" if changed else f"RSVP'd {word} - {inv.title}")[:250]
    address = (inv.linked_property or {}).get("address") or inv.address
    snapshot = {
        "invite_id": inv.id, "invite_code": inv.code, "invite_title": inv.title, "invite_type": inv.invite_type,
        "response": rsvp.response, "guest_count": rsvp.guest_count, "answers": [*(rsvp.answers or []), *builtin_answers(rsvp)],
        "event_date": inv.event_date.isoformat() if inv.event_date else None, "days": list(rsvp.days or []),
        "lead_kind": kind, "still_interested": rsvp.still_interested, "keep_updated": rsvp.keep_updated,
    }
    represented = represented_of(inv.questions or [], {a["questionId"]: a["value"] for a in rsvp.answers or []}, rsvp.has_agent)
    try:
        from app.models.database import Lead
        lead = await db.get(Lead, rsvp.lead_id) if rsvp.lead_id else None
        lead = lead or await existing_lead(db, owner.id, email=rsvp.email, phone=rsvp.phone, name=rsvp.name)
        if lead is not None and not (rsvp.email and (lead.email or "").lower() == rsvp.email.lower()):
            # Matched on phone + name (or an earlier RSVP's lead): onto that lead's page.
            if not lead.email and rsvp.email:
                lead.email = rsvp.email.lower()
            if represented and (lead.represented in (None, "unknown") or rsvp.has_agent is not None):
                lead.represented = represented
            db.add(lead)
            event = await log_activity(db, owner_id=owner.id, email=None, lead=lead, source_type="invite_rsvp", summary=summary,
                                       source_id=rsvp.id, property_address=address, raw_snapshot=snapshot)
            if event is not None and kind == "lead":
                from app.services.tasks_service import schedule_followup_for_lead_event
                schedule_followup_for_lead_event(owner.id, lead.id, event.id, "invite_rsvp")
        elif lead is not None or kind == "lead":
            # Same email as a lead they have (a non-lead's RSVP lands on it - no follow-up, see
            # tasks_service), or a brand-new real lead.
            lead = await upsert_lead(
                db, owner_id=owner.id, name=rsvp.name, email=rsvp.email, phone=rsvp.phone,
                source_type="invite_rsvp", source_id=rsvp.id, summary=summary, occurred_at=datetime.now(timezone.utc),
                represented=represented, override_represented=rsvp.has_agent is not None,
                property_address=address, raw_snapshot=snapshot,
            )
        else:
            return rsvp.lead_id
    except Exception:  # noqa: BLE001 - the RSVP itself is already saved
        logger.warning("invite rsvp lead failed", exc_info=True)
        await db.rollback()
        return rsvp.lead_id
    if lead is not None and rsvp.lead_id != lead.id:
        rsvp.lead_id = lead.id
        db.add(rsvp)
        await db.commit()
    return lead.id if lead is not None else rsvp.lead_id


async def email_guest_confirmation(db: AsyncSession, owner: User, inv: EventInvite, rsvp: EventInviteRsvp, link: str) -> None:
    """The guest's short "you're on the list" note (+ a calendar invite when they're coming)."""
    if not rsvp.email or not _EMAIL_RE.match(rsvp.email):
        return
    try:
        from app.services.blacklist_service import BlacklistService
        if await BlacklistService.is_blacklisted(db, rsvp.email):
            return
        from app.services.agent_email_service import email_person
        from app.services.email_avatar_service import agent_email_vars
        agent = await agent_email_vars(db, owner)
        days = rsvp_days(inv, rsvp)
        variables = {
            **agent, "first_name": (rsvp.name or "").split(" ")[0] or "there",
            "invite_title": inv.title, "invite_subtitle": line_two(inv), "event_phrase": event_phrase(inv), "invite_when": when_label(inv),
            "days_label": days_label(days) if rsvp.response != "no" else "", "invite_place": place_label(inv),
            "invite_link": f"{link}?r={rsvp.token}", "rsvp_response": rsvp.response,
            "guest_count": rsvp.guest_count, "still_interested": bool(rsvp.still_interested), "keep_updated": bool(rsvp.keep_updated),
        }
        if rsvp.response != "no":
            ics = ics_text(inv, agent_name=agent.get("agent_name") or "", url=link, method="REQUEST", only=[d["date"] for d in days])
            if ics:
                variables["_ics"] = ics
        subject = {"yes": f"You're on the list - {inv.title}", "maybe": f"Got it - {inv.title}",
                   "no": f"Thanks for letting me know - {inv.title}"}[rsvp.response]
        email_person(rsvp.email, "event_rsvp", subject, variables,
                     from_name=agent.get("agent_name") or "OpenHousePal", owner_id=owner.id)
    except Exception:  # noqa: BLE001
        logger.warning("invite rsvp confirmation email failed", exc_info=True)


async def send_invitations(db: AsyncSession, owner: User, inv: EventInvite, recipients: list[tuple[str, Optional[str]]],
                           link: str, *, resend: bool = False) -> dict:
    """Emails the invitation (a short personal note with the link) to each address. Skips bad and
    unsubscribed addresses, and anyone already emailed in the last 12 hours on a resend."""
    from app.services.agent_email_service import email_person
    from app.services.blacklist_service import BlacklistService
    from app.services.email_avatar_service import agent_email_vars
    from app.services.agent_email_service import app_url
    from urllib.parse import quote

    existing = {r.email: r for r in (await db.execute(
        select(EventInviteRecipient).where(EventInviteRecipient.invite_id == inv.id))).scalars().all()}
    total = len(existing)
    agent = await agent_email_vars(db, owner)
    now = datetime.now(timezone.utc)
    sent, skipped = [], []
    seen: set[str] = set()
    for email, name in recipients:
        e = (email or "").strip().lower()
        if not e or e in seen:
            continue
        seen.add(e)
        if not _EMAIL_RE.match(e):
            skipped.append({"email": e, "reason": "not a valid email"})
            continue
        if await BlacklistService.is_blacklisted(db, e):
            skipped.append({"email": e, "reason": "unsubscribed"})
            continue
        rec = existing.get(e)
        if rec is None and total >= MAX_RECIPIENTS_PER_INVITE:
            skipped.append({"email": e, "reason": "invite list is full"})
            continue
        if resend and rec is not None and rec.last_sent_at and now - rec.last_sent_at < RESEND_GAP:
            skipped.append({"email": e, "reason": "emailed in the last 12 hours"})
            continue
        if rec is None:
            rec = EventInviteRecipient(invite_id=inv.id, email=e, name=(name or "").strip()[:140] or None, send_count=0)
            existing[e] = rec
            total += 1
        rec.send_count = (rec.send_count or 0) + 1
        rec.last_sent_at = now
        db.add(rec)
        first = (rec.name or "").split(" ")[0]
        email_person(e, "event_invite", inv.title, {
            **agent, "first_name": first or "there", "invite_title": inv.title, "invite_subtitle": line_two(inv), "invite_when": when_label(inv),
            "invite_place": place_label(inv), "invite_message": inv.message or "", "invite_link": link,
            "is_reminder": resend, "Unsub": f"{app_url()}/unsubscribe?email={quote(e, safe='')}",
        }, from_name=agent.get("agent_name") or "OpenHousePal", owner_id=owner.id)
        sent.append(e)
    await db.commit()
    return {"sent": len(sent), "sent_to": sent, "skipped": skipped}


async def non_responders(db: AsyncSession, inv: EventInvite) -> list[tuple[str, Optional[str]]]:
    answered = {(e or "").lower() for e in (await db.execute(
        select(EventInviteRsvp.email).where(EventInviteRsvp.invite_id == inv.id))).scalars().all() if e}
    recs = (await db.execute(select(EventInviteRecipient).where(EventInviteRecipient.invite_id == inv.id))).scalars().all()
    return [(r.email, r.name) for r in recs if r.email not in answered]


# ------------------------------------------------------------------------------------- the date step's "already scheduled"

def _hm(t: Any) -> Optional[str]:
    if t is None:
        return None
    if hasattr(t, "strftime"):
        return t.strftime("%H:%M")
    s = str(t)
    return s[:5] if re.match(r"^\d{2}:\d{2}", s) else None


async def scheduled_slots(db: AsyncSession, owner: User, *, listing_key: Optional[str], address: Optional[str]) -> dict:
    """Open houses already on the calendar for this home (user, 10/24: "offer those first so the
    agent doesn't re-pick the date and time"): the agent's own scheduled open houses in
    OpenHousePal for it, then Bright's (Property.mls_open_houses). Soonest first, no repeats.
    -> {"slots": [{date, start, end, source: 'ohp' | 'bright'}], "open_house_event_id": the agent's
    open house (kit) for this home, for the Cheat Sheet + the guest's requests}."""
    from app.models.database import OpenHouseEvent, Property
    from app.services.mls_open_houses import upcoming_mls_open_houses
    today = datetime.now(EASTERN).date()
    out: list[dict] = []
    street = (address or "").split(",")[0].strip().lower()
    if not listing_key and not street:
        return {"slots": [], "open_house_event_id": None}
    mine = select(OpenHouseEvent).where(OpenHouseEvent.agent_id == owner.id, OpenHouseEvent.is_deleted.isnot(True))
    mine = mine.where(OpenHouseEvent.listing_key == listing_key) if listing_key else mine.where(func.lower(OpenHouseEvent.address).like(f"{street}%"))
    ohs = (await db.execute(mine.order_by(OpenHouseEvent.created_at.desc()).limit(40))).scalars().all()
    for oh in ohs:
        if oh.scheduled_date and oh.scheduled_date >= today:
            out.append({"date": oh.scheduled_date.isoformat(), "start": _hm(oh.scheduled_start_time), "end": _hm(oh.scheduled_end_time), "source": "ohp"})
    kit_id = ohs[0].id if ohs else None
    if listing_key:
        prop = (await db.execute(select(Property).where(Property.listing_key == listing_key))).scalars().first()
        for s in (upcoming_mls_open_houses(prop, today) if prop is not None else []):
            iso = s["date"].isoformat()
            if not any(x["date"] == iso for x in out):
                out.append({"date": iso, "start": _hm(s.get("start")), "end": _hm(s.get("end")), "source": "bright"})
    return {"slots": sorted(out, key=lambda x: (x["date"], x.get("start") or ""))[:6], "open_house_event_id": kit_id}


# ------------------------------------------------------------------------------------- the guest page's extras

async def cheat_sheet_of(db: AsyncSession, inv: EventInvite) -> Optional[dict]:
    """The open house's Cheat Sheet (the same saved snapshot the Guest Pass shows), when the invite
    belongs to one of the agent's open houses."""
    if not inv.open_house_event_id:
        return None
    from app.models.database import OpenHouseEvent
    oh = await db.get(OpenHouseEvent, inv.open_house_event_id)
    if oh is None or oh.agent_id != inv.owner_id or oh.is_deleted:
        return None
    snap = oh.cheat_sheet_snapshot or {}
    if not isinstance(snap, dict) or not snap.get("property"):
        return None
    return {"property": {**snap["property"], "openHouseEventId": oh.id}, "amenities": snap.get("amenities")}


def _first_words(v: Any, n: int = 3) -> Optional[str]:
    if isinstance(v, list):
        items = [str(x).strip() for x in v if str(x).strip()]
        return ", ".join(items[:n]) or None
    s = str(v or "").strip()
    return s or None


async def home_details_of(db: AsyncSession, inv: EventInvite) -> Optional[dict]:
    """More about the home for the guest page (owner, 10/24 round 2: "these pages feel super
    empty"): the listing's description, key facts, schools and where it is on a map. From the
    synced listing (Property) when the invite is about one of the agent's MLS homes; the open
    house's Cheat Sheet highlights paragraph wins over the plain description when there is one."""
    lp = inv.linked_property or {}
    if not lp.get("listing_key"):
        return None
    from app.models.database import Property
    p = (await db.execute(select(Property).where(Property.listing_key == lp["listing_key"]))).scalars().first()
    if p is None:
        return None
    highlights = None
    cheat = await cheat_sheet_of(db, inv)
    if cheat and isinstance(cheat.get("property"), dict):
        hl = cheat["property"].get("descriptionHighlights")
        if isinstance(hl, str) and hl.strip():
            highlights = hl.strip()
        elif isinstance(hl, list) and hl:
            highlights = " ".join(str(x).strip() for x in hl if str(x).strip())
    desc = (p.description or "").strip()
    if len(desc) > 700:
        cut = desc[:700]
        desc = cut[: cut.rfind(". ") + 1] if ". " in cut else cut.rsplit(" ", 1)[0] + "…"
    facts = [
        ("Year built", str(p.year_built) if p.year_built else None),
        ("Lot", f"{round(float(p.lot_size_acres), 2)} acres" if p.lot_size_acres else None),
        ("Style", _first_words(p.architectural_style, 2) or (p.home_type or "").replace("_", " ").title() or None),
        ("Garage", f"{int(p.garage_spaces)} car" if p.garage_spaces else None),
        ("Basement", _first_words(p.basement, 2)),
        ("Heating", _first_words(p.heating, 2)),
        ("Cooling", _first_words(p.cooling, 2)),
        ("Taxes", f"${int(p.tax_annual_amount):,}/yr" if p.tax_annual_amount else None),
        ("HOA", f"${int(p.association_fee):,}" + (f" / {str(p.association_fee_frequency).lower()}" if p.association_fee_frequency else "") if p.association_fee else None),
        ("Neighborhood", (p.subdivision_name or "").title() or None),
    ]
    schools = [x for x in ((p.elementary_school, "Elementary"), (p.middle_or_junior_school, "Middle"), (p.high_school, "High")) if x[0]]
    return {
        "highlights": highlights, "description": desc or None,
        "facts": [{"label": k, "value": v} for k, v in facts if v][:8],
        "schools": [{"level": lvl, "name": str(name).title() if str(name).isupper() else str(name)} for name, lvl in schools],
        "school_district": (p.school_district_name or "").title() or None,
        "township": (p.township or "").title() or None,
        "latitude": p.latitude, "longitude": p.longitude,
        "features": [str(x) for x in (p.interior_features or [])[:6]] if isinstance(p.interior_features, list) else [],
    }


REQUEST_WORDS = {"disclosure": "the seller disclosure", "second_tour": "a second tour", "updates": "updates on the home"}


async def public_request(db: AsyncSession, owner: User, inv: EventInvite, payload, *, quiet: bool = False) -> dict:
    """The Guest Pass's one-tap asks, from the invitation: the seller disclosure / a second tour go
    through the open house's own request route when the invite belongs to one (same lead activity,
    bell, email and follow-up as the Cheat Sheet's QR codes); "keep me updated" follows the home on
    its Spotlight site when it has one (price / open house / under contract emails), else the agent
    is told. A repeat tap never sends it twice. Raises ValueError with a guest-facing message."""
    from app.models.database import OpenHouseEvent, PropertyWebsite
    from app.services.agent_email_service import absolute, email_agent
    from app.services.leads_service import upsert_lead
    kind = payload.kind
    rsvp = None
    if payload.token:
        rsvp = (await db.execute(select(EventInviteRsvp).where(EventInviteRsvp.token == payload.token,
                                                               EventInviteRsvp.invite_id == inv.id))).scalar_one_or_none()
    name = ((rsvp.name if rsvp else None) or payload.name or "").strip()[:140]
    email = ((rsvp.email if rsvp else None) or payload.email or "").strip().lower()[:200] or None
    phone = ((rsvp.phone if rsvp else None) or payload.phone or "").strip()[:40] or None
    if not name:
        raise ValueError("Add your name.")
    if email and not _EMAIL_RE.match(email):
        raise ValueError("Enter a valid email address.")
    if kind in ("disclosure", "updates") and not email:
        raise ValueError("Add your email so it can be sent to you.")
    if not email and not phone:
        raise ValueError("Add an email or phone so the host can reach you.")
    if rsvp is not None and (rsvp.requests or {}).get(kind):
        return {"sent": True, "already": True}
    now = datetime.now(timezone.utc)
    address = (inv.linked_property or {}).get("address") or inv.address
    times = (payload.times or "").strip()[:200] or None

    if kind in ("disclosure", "second_tour"):
        oh = await db.get(OpenHouseEvent, inv.open_house_event_id) if inv.open_house_event_id else None
        if oh is not None and oh.agent_id == owner.id and not oh.is_deleted and email:
            from app.api.open_houses_routes import submit_open_house_form_request
            from app.schemas.open_house import OpenHouseFormRequestCreate, OpenHouseFormRequestType
            await submit_open_house_form_request(oh.id, OpenHouseFormRequestCreate(
                form_type=OpenHouseFormRequestType("disclosure" if kind == "disclosure" else "second_visit"),
                full_name=name, email=email, phone=phone or "",
                preferred_visit_time=times if kind == "second_tour" else None,
                source="invite",
            ), db)
        else:
            st = "open_house_disclosure" if kind == "disclosure" else "open_house_second_visit"
            what = "Requested the seller disclosure from your invite" if kind == "disclosure" else "Requested a second visit from your invite"
            lead = await upsert_lead(
                db, owner_id=owner.id, name=name, email=email, phone=phone, source_type=st, source_id=inv.id,
                summary=f"{what} - {address or inv.title}"[:250], occurred_at=now, property_address=address,
                raw_snapshot={"answers": [{"label": "Preferred visit time", "value": times}] if times else [],
                              "invite_id": inv.id, "from": "invite"},
            )
            ntype = "OPEN_HOUSE_DISCLOSURE" if kind == "disclosure" else "OPEN_HOUSE_SECOND_VISIT"
            title = f"{'Disclosure Request' if kind == 'disclosure' else 'Second Visit Request'}: {name}"
            to = f"/leads/{lead.id}" if lead is not None else invite_link(inv)
            try:
                db.add(Notification(agent_id=owner.id, type=ntype, reference_type="INVITE_RSVP", reference_id=(rsvp.id if rsvp else inv.id),
                                    title=title[:200], message=f"Asked for {REQUEST_WORDS[kind]} from your invite - {inv.title}"[:500],
                                    property_address=address, visitor_name=name, link=to, is_read=False, created_at=now))
                await db.commit()
            except Exception:  # noqa: BLE001
                await db.rollback()
            email_agent(owner, ntype, "open_house_form_request", f"{name} requested {REQUEST_WORDS[kind]}",
                        {"visitor_name": name, "request_label": REQUEST_WORDS[kind], "property_address": address or inv.title,
                         "visitor_email": email, "visitor_phone": phone, "details": f"Preferred visit time: {times}" if times else None,
                         "link": absolute(to)})
    else:  # updates
        lp = inv.linked_property or {}
        site = None
        if lp.get("listing_key"):
            site = (await db.execute(select(PropertyWebsite).where(PropertyWebsite.owner_id == owner.id,
                                                                   PropertyWebsite.listing_key == lp["listing_key"],
                                                                   PropertyWebsite.status == "PUBLISHED"))).scalars().first()
        if site is not None:
            from app.services import spotlight_alerts_service
            await spotlight_alerts_service.subscribe(db, site, name=name, email=email, phone=phone,
                                                     came_from={"src": "invite", "invite": inv.code})
        elif not quiet:  # (from the RSVP itself, the RSVP's own alert already says it)
            try:
                street = (address or inv.title).split(",")[0]
                db.add(Notification(agent_id=owner.id, type="INVITE_RSVP", reference_type="INVITE_RSVP",
                                    reference_id=(rsvp.id if rsvp else inv.id), title=f"{name} wants updates on {street}"[:200],
                                    message=f"Asked to be kept updated on the home - from your invite {inv.title}"[:500],
                                    property_address=address, visitor_name=name, link=invite_link(inv), is_read=False, created_at=now))
                await db.commit()
            except Exception:  # noqa: BLE001
                await db.rollback()
        if rsvp is not None:
            rsvp.keep_updated = True
    if rsvp is not None:
        rsvp.requests = {**(rsvp.requests or {}), kind: {"at": now.isoformat(), **({"times": times} if times else {})}}
        rsvp.is_seen = False
        db.add(rsvp)
        await db.commit()
    return {"sent": True, "already": False}


# ------------------------------------------------------------------------------------- the agent's "tomorrow" email

async def send_day_before(db: AsyncSession, now: Optional[datetime] = None, *, owner_ids: Optional[list[str]] = None) -> int:
    """Once per day of each live invite, from 9 AM Eastern the day before: the agent's "Tomorrow"
    email - who's coming, how many, the maybes and anything they asked for (user, 10/24). Runs
    hourly (a restart or a late-made invite still gets it); agent_reminded remembers the days done.
    Respects the agent's Invite RSVP email switch. -> emails sent."""
    from app.services.agent_email_service import absolute, app_url, email_agent
    from app.utils.auth import owner_has_access
    now_et = (now or datetime.now(timezone.utc)).astimezone(EASTERN)
    if now_et.hour < 9:
        return 0
    today = now_et.date()
    tomorrow = today + timedelta(days=1)
    t_iso = tomorrow.isoformat()
    q = select(EventInvite).where(EventInvite.status == "live", EventInvite.event_date.isnot(None),
                                  EventInvite.event_date <= tomorrow, EventInvite.event_date >= today - timedelta(days=40))
    if owner_ids is not None:
        q = q.where(EventInvite.owner_id.in_(owner_ids))
    sent = 0
    for inv in (await db.execute(q)).scalars().all():
        days = [d for d in all_days(inv) if d["date"] == tomorrow]
        if not days or t_iso in (inv.agent_reminded or []):
            continue
        inv.agent_reminded = [*(inv.agent_reminded or []), t_iso][-14:]
        db.add(inv)
        await db.commit()
        owner = await db.get(User, inv.owner_id)
        if owner is None or not owner_has_access(owner):
            continue
        rsvps = (await db.execute(select(EventInviteRsvp).where(EventInviteRsvp.invite_id == inv.id)
                                  .order_by(EventInviteRsvp.created_at.asc()))).scalars().all()

        def on_day(r: EventInviteRsvp) -> bool:
            chosen = {d for d in (_as_date(x) for x in (r.days or [])) if d}
            return not chosen or tomorrow in chosen

        def row(r: EventInviteRsvp) -> dict:
            tag = {"agent": "agent", "represented": "has an agent", "lead": "new lead"}.get(r.lead_kind or "", "")
            asked = [REQUEST_WORDS[k].replace("the ", "") for k in (r.requests or {}) if k in ("disclosure", "second_tour")]
            return {"name": r.name or "Guest", "guest_count": r.guest_count, "tag": tag,
                    "asked": f"asked for {' & '.join(asked)}" if asked else ""}

        coming = [row(r) for r in rsvps if r.response == "yes" and on_day(r)]
        maybe = [row(r) for r in rsvps if r.response == "maybe" and on_day(r)]
        guests = sum(r.guest_count or 1 for r in rsvps if r.response == "yes" and on_day(r))
        email_agent(owner, "INVITE_RSVP", "invite_day_before_agent",
                    f"Tomorrow: {guests} coming to {inv.title}" if guests else f"Tomorrow: {inv.title}", {
                        "invite_title": inv.title, "day_label": day_label(days[0]), "place": place_label(inv),
                        "coming": coming, "maybe": maybe, "guests_coming": guests,
                        "link": absolute(invite_link(inv)), "app_url": app_url(),
                    })
        sent += 1
    return sent


async def scheduled_day_before() -> None:
    """APScheduler entry point (app/main.py) - hourly."""
    from app.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            n = await send_day_before(db)
            if n:
                logger.info("invite day-before emails sent", extra={"count": n})
    except Exception:  # noqa: BLE001
        logger.warning("invite day-before job failed", exc_info=True)
