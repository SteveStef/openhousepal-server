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
            _q("party", "select", "How many of you are coming?", PARTY, required=True, role="guests"),
            _q("agent", "select", "Are you working with a real estate agent?", ["Yes", "No"], role="represented"),
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
QUESTION_TYPES = {"short_text", "long_text", "select", "multi_select", "number", "phone", "email", "date"}


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


def when_label(inv: EventInvite) -> str:
    """"Saturday, November 8 · 1 - 3 PM"."""
    if not inv.event_date:
        return ""
    d = inv.event_date
    day = f"{d.strftime('%A, %B')} {d.day}"
    if inv.start_time and inv.end_time:
        a, b = clock(inv.start_time), clock(inv.end_time)
        rng = f"{a[:-3]} - {b}" if a[-2:] == b[-2:] else f"{a} - {b}"
        return f"{day} · {rng}"
    if inv.start_time:
        return f"{day} · {clock(inv.start_time)}"
    return day


def is_past(inv: EventInvite) -> bool:
    if not inv.event_date:
        return False
    return inv.event_date < datetime.now(EASTERN).date()


def place_label(inv: EventInvite) -> str:
    return ", ".join(x for x in (inv.place_name, inv.address) if x) or ""


def _start_end(inv: EventInvite) -> tuple[Optional[datetime], Optional[datetime]]:
    if not inv.event_date:
        return None, None
    try:
        hh, mm = (int(x) for x in (inv.start_time or "18:00").split(":"))
        start = datetime(inv.event_date.year, inv.event_date.month, inv.event_date.day, hh, mm, tzinfo=EASTERN)
        if inv.end_time:
            eh, em = (int(x) for x in inv.end_time.split(":"))
            end = datetime(inv.event_date.year, inv.event_date.month, inv.event_date.day, eh, em, tzinfo=EASTERN)
            if end <= start:
                end = start + timedelta(hours=2)
        else:
            end = start + timedelta(hours=2)
        return start, end
    except (TypeError, ValueError):
        return None, None


def ics_text(inv: EventInvite, *, agent_name: str, url: str, method: str = "PUBLISH") -> Optional[str]:
    """An .ics for "Add to calendar" (PUBLISH) or attached to the confirmation email (REQUEST)."""
    from app.services.calendar_invite_service import _esc, _fold, _utc
    start, end = _start_end(inv)
    if not start or not end:
        return None
    desc = " ".join(x for x in ((inv.message or "").strip(), f"Hosted by {agent_name}." if agent_name else "", url) if x)
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//OpenHousePal//Invites//EN", "CALSCALE:GREGORIAN", f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:invite-{inv.id}@openhousepal.com",
        f"DTSTAMP:{_utc(datetime.now(timezone.utc))}",
        f"DTSTART:{_utc(start)}",
        f"DTEND:{_utc(end)}",
        f"SUMMARY:{_esc(inv.title)}",
        f"DESCRIPTION:{_esc(desc)}",
        f"LOCATION:{_esc(place_label(inv))}",
        f"URL:{url}",
        "SEQUENCE:0", "STATUS:CONFIRMED", "TRANSP:OPAQUE",
        "END:VEVENT", "END:VCALENDAR",
    ]
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


def represented_of(questions: list[dict], answers: dict[str, Any]) -> Optional[str]:
    for q in questions:
        if q.get("role") == "represented":
            v = str(answers.get(q["id"]) or "").strip().lower()
            if v.startswith("yes"):
                return "yes"
            if v.startswith("no"):
                return "no"
    return None


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
        "title": inv.title, "message": inv.message,
        "event_date": inv.event_date.isoformat() if inv.event_date else None,
        "start_time": inv.start_time, "end_time": inv.end_time,
        "place_name": inv.place_name, "address": inv.address,
        "linked_property": inv.linked_property, "cover_image_url": inv.cover_image_url,
        "design": inv.design, "accent_color": inv.accent_color or DEFAULT_ACCENT,
        "questions": inv.questions or [], "status": inv.status, "view_count": inv.view_count or 0,
        "when_label": when_label(inv), "is_past": is_past(inv),
        "share_url": share_url, "platform_url": platform_url(inv.code),
        "created_at": inv.created_at.isoformat() if inv.created_at else None,
        "updated_at": inv.updated_at.isoformat() if inv.updated_at else None,
        "counts": counts or {"yes": 0, "maybe": 0, "no": 0, "guests_yes": 0, "guests_maybe": 0, "unseen": 0, "last_rsvp_at": None},
    }


def rsvp_out(r: EventInviteRsvp, *, private: bool = True) -> dict:
    out = {
        "id": r.id, "response": r.response, "name": r.name, "guest_count": r.guest_count,
        "answers": r.answers or [],
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }
    if private:
        out.update({"email": r.email, "phone": r.phone, "lead_id": r.lead_id, "is_seen": r.is_seen})
    return out


def apply_save(inv: EventInvite, payload, accent_default: str) -> None:
    inv.invite_type = payload.invite_type
    inv.audience = payload.audience or PRESETS[payload.invite_type]["audience"]
    if payload.invite_type == "brokers_open":
        inv.audience = "agents"
    inv.title = payload.title.strip()[:140]
    inv.message = (payload.message or "").strip()[:2000] or None
    inv.event_date = payload.event_date
    inv.start_time = payload.start_time
    inv.end_time = payload.end_time if payload.start_time else None
    inv.place_name = (payload.place_name or "").strip()[:140] or None
    inv.address = (payload.address or "").strip()[:300] or None
    lp = payload.linked_property.model_dump(exclude_none=True) if payload.linked_property else None
    if lp and isinstance(lp.get("photos"), list):
        lp["photos"] = [str(p) for p in lp["photos"] if p][:12]
    inv.linked_property = lp or None
    cover = (payload.cover_image_url or "").strip()
    inv.cover_image_url = cover if cover and (cover.startswith("http") or cover.startswith("/uploads/")) else None
    inv.design = payload.design if payload.design in DESIGNS else "classic"
    inv.accent_color = clean_accent(payload.accent_color) or accent_default
    inv.questions = clean_questions(payload.questions)


# ------------------------------------------------------------------------------------- RSVP side effects

async def notify_agent(db: AsyncSession, owner: User, inv: EventInvite, rsvp: EventInviteRsvp, *, changed: bool, lead_id: Optional[str]) -> None:
    """The bell + the agent's "New leads & sign-ins" email. Never raises."""
    from app.services.agent_email_service import absolute, email_agent
    word = {"yes": "is coming", "maybe": "might come", "no": "can't make it"}[rsvp.response]
    name = rsvp.name or "A guest"
    extra = f" (+{rsvp.guest_count - 1})" if rsvp.response != "no" and rsvp.guest_count > 1 else ""
    title = f"{'Updated RSVP' if changed else 'New RSVP'}: {name}{extra} {word}"[:200]
    link = f"/studio/forms?view=invites&invite={inv.id}"
    try:
        db.add(Notification(
            agent_id=owner.id, type="INVITE_RSVP", reference_type="INVITE_RSVP", reference_id=rsvp.id,
            title=title, message=f"{name} {word} - {inv.title}"[:500], visitor_name=name,
            property_address=(inv.linked_property or {}).get("address") or inv.address,
            link=link, is_read=False, created_at=datetime.now(timezone.utc),
        ))
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("invite rsvp bell failed", exc_info=True)
        await db.rollback()
    lines = [f"RSVP: {'Yes' if rsvp.response == 'yes' else 'Maybe' if rsvp.response == 'maybe' else 'No'}"
             + (f" · {rsvp.guest_count} coming" if rsvp.response != "no" else "")]
    for a in rsvp.answers or []:
        lines.append(f"{a.get('label')}: {_answer_text(a.get('value'))}")
    email_agent(owner, "INVITE_RSVP", "new_lead_captured", title, {
        "lead_name": name,
        "lead_source": f"Invite RSVP - {inv.title}",
        "lead_email": rsvp.email or "", "lead_phone": rsvp.phone or "",
        "lead_message": "\n".join(lines)[:1500],
        "property_address": (inv.linked_property or {}).get("address") or inv.address or inv.title,
        "link": absolute(f"/leads/{lead_id}" if lead_id else link),
    })


async def attach_lead(db: AsyncSession, owner: User, inv: EventInvite, rsvp: EventInviteRsvp, *, changed: bool) -> Optional[str]:
    """Client invites only: the guest becomes (or joins) a Lead with the 'invite_rsvp' source."""
    if inv.audience != "clients" or not (rsvp.email or rsvp.phone):
        return rsvp.lead_id
    from app.services.leads_service import upsert_lead
    word = {"yes": "Coming", "maybe": "Maybe", "no": "Can't make it"}[rsvp.response]
    summary = f"Changed RSVP to {word} - {inv.title}" if changed else f"RSVP'd {word} - {inv.title}"
    try:
        lead = await upsert_lead(
            db, owner_id=owner.id, name=rsvp.name, email=rsvp.email, phone=rsvp.phone,
            source_type="invite_rsvp", source_id=rsvp.id, summary=summary[:250],
            occurred_at=datetime.now(timezone.utc),
            represented=represented_of(inv.questions or [], {a["questionId"]: a["value"] for a in rsvp.answers or []}),
            property_address=(inv.linked_property or {}).get("address") or inv.address,
            raw_snapshot={
                "invite_id": inv.id, "invite_code": inv.code, "invite_title": inv.title, "invite_type": inv.invite_type,
                "response": rsvp.response, "guest_count": rsvp.guest_count, "answers": rsvp.answers or [],
                "event_date": inv.event_date.isoformat() if inv.event_date else None,
            },
        )
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
        variables = {
            **agent, "first_name": (rsvp.name or "").split(" ")[0] or "there",
            "invite_title": inv.title, "invite_when": when_label(inv), "invite_place": place_label(inv),
            "invite_link": f"{link}?r={rsvp.token}", "rsvp_response": rsvp.response,
            "guest_count": rsvp.guest_count,
        }
        if rsvp.response != "no":
            ics = ics_text(inv, agent_name=agent.get("agent_name") or "", url=link, method="REQUEST")
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
            **agent, "first_name": first or "there", "invite_title": inv.title, "invite_when": when_label(inv),
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
