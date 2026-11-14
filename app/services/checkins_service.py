"""Check-ins (owner, 11/13): who an agent with a big book should reach back out to, and exactly why.

Only a lead with a real reason gets one - never a blanket "check in with everyone":
  - they've been a lead for at least a week, and the agent hasn't reached out to them for a week;
  - something worth it: the agent left a note on them, they opened / clicked the agent's emails, or they
    used their Showcase (liked, commented on, looked at homes) since the agent last reached out, or they
    came to more than one of the agent's open houses;
  - nothing already covers it: no open follow-up for them, and whatever they did before the last follow-up
    was made counts as handled by it (a "homes they liked" follow-up already covered those likes);
  - not signed / hidden / archived, not one the agent stopped follow-ups for, and reachable (phone or email);
  - someone with their own agent only when they came to the agent's own listing or the agent left a note.

Each one is scored by how likely they are to answer (no agent + a note + opening emails + using their
Showcase, not heard from the agent in a week or two = top), and the reasons come out strongest first, in
plain words ("You left a note on Jane at your 417 Dogwood Ln open house: ..."). No AI - free to show.
The top 3 are "Recommended", then "Worth a check-in", then "Less likely to respond".

The scoring + wording is pure (evaluate) so it's tested without a database; gather() reads the signals."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger

logger = get_logger(__name__)

MIN_LEAD_AGE_DAYS = 7        # a lead for at least a week ...
QUIET_FROM_AGENT_DAYS = 7    # ... and no word from the agent for a week
ACTIVITY_WINDOW_DAYS = 45    # opens / Showcase use count when this recent (and since the agent's last touch)
NOTE_WINDOW_DAYS = 180       # a note older than this (with nothing since) isn't a reason on its own
EVENT_LOOKBACK_DAYS = 365
DONE_STATUSES = {"signed", "hidden", "converted", "archived"}
SKIP_KEY = "_checkin_skip"   # User.followup_rules[SKIP_KEY] = {lead_id: "until" ISO} - "Not now"
SKIP_DAYS = 30
TOP_N = 3
RECOMMENDED_MIN = 45         # the top 3 have to be genuinely good, not just the best of a weak list
WORTH_MIN = 30
LIMIT = 60

GROUPS = [
    {"key": "recommended", "label": "Recommended", "hint": "Most likely to answer - start here"},
    {"key": "worth", "label": "Worth a check-in", "hint": "Real reasons to reach back out"},
    {"key": "less", "label": "Less likely to respond", "hint": "Quieter, but you have a reason"},
]


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _eastern(dt: datetime):
    from app.services.tasks_service import EASTERN
    return _utc(dt).astimezone(EASTERN)


def when_phrase(dt: Optional[datetime], now: datetime) -> str:
    """'today' / 'yesterday' / 'on Tuesday' (this past week) / 'on Oct 25'."""
    if dt is None:
        return ""
    d, today = _eastern(dt).date(), _eastern(now).date()
    days = (today - d).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"on {d.strftime('%A')}"
    return f"on {d.strftime('%b')} {d.day}"


def gap_phrase(days: int) -> str:
    if days < 14:
        return "over a week"
    if days < 30:
        return f"{days // 7} weeks"
    months = days // 30
    return "about a month" if months <= 1 else f"{months} months"


def _street(address: Optional[str]) -> str:
    from app.services.email_service import tidy_caps
    s = (address or "").split(",")[0].strip()
    return tidy_caps(s) if s else ""


def _join(items: List[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return items[0] if items else ""
    return f"{', '.join(items[:-1])} and {items[-1]}"


def note_snippet(text: str, limit: int = 70) -> str:
    t = " ".join((text or "").split())
    if len(t) <= limit:
        return t
    cut = t[:limit].rsplit(" ", 1)[0].rstrip(",.;:-")
    return f"{cut}…"


@dataclass
class Signals:
    """What we know about one lead, already trimmed to what counts (activity since the agent's last touch)."""
    lead_id: str
    name: str
    first: str
    represented: str = "unknown"              # yes | no | unknown
    status: str = "new"
    has_phone: bool = False
    has_email: bool = False
    created_at: Optional[datetime] = None
    last_touch: Optional[datetime] = None     # the agent last reached out (contacted / checked a follow-up off)
    ever_contacted: bool = False
    # [{"text", "at", "street", "own_listing", "open_house": bool}] newest first
    notes: List[Dict[str, Any]] = field(default_factory=list)
    # [{"street", "at", "own_listing"}] newest first - open house sign-ins (all time)
    visits: List[Dict[str, Any]] = field(default_factory=list)
    emails_opened: int = 0                    # emails opened since the last touch
    open_total: int = 0                       # opens of those emails (one email opened 3 times = 3)
    last_open: Optional[datetime] = None
    clicks: int = 0                           # emails with a link clicked since the last touch
    last_click: Optional[datetime] = None
    liked: List[str] = field(default_factory=list)       # streets, newest first, each once (not since passed on)
    last_like: Optional[datetime] = None
    commented: List[str] = field(default_factory=list)
    last_comment: Optional[datetime] = None
    viewed: List[str] = field(default_factory=list)      # streets looked at (not liked)
    last_view: Optional[datetime] = None
    timeline: Optional[str] = None            # their sign-in / form answer
    pre_approved: Optional[str] = None


_SOON = ("right away", "asap", "1-3 months", "1–3 months", "0-3 months", "immediately")


def _intent_bits(s: Signals) -> List[tuple]:
    out = []
    pa = (s.pre_approved or "").strip().lower()
    if pa.startswith("yes"):
        out.append((8, "Pre-approved"))
    elif "cash" in pa:
        out.append((8, "Paying cash"))
    tl = (s.timeline or "").strip().lower()
    if tl and any(w in tl for w in _SOON):
        out.append((8, "Buying in 1-3 months" if "month" in tl else "Buying right away"))
    elif "research" in tl:
        out.append((-5, None))
    return out


def evaluate(s: Signals, now: datetime) -> Optional[Dict[str, Any]]:
    """Score + reasons for one lead, or None when there's no real reason to check in."""
    now = _utc(now)
    created = _utc(s.created_at) or now
    if now - created < timedelta(days=MIN_LEAD_AGE_DAYS):
        return None
    touch = _utc(s.last_touch) or created
    quiet_days = (now - touch).days
    if quiet_days < QUIET_FROM_AGENT_DAYS:
        return None
    if not (s.has_phone or s.has_email):
        return None
    who = s.first or "them"
    own_visit = next((v for v in s.visits if v.get("own_listing")), None)
    fresh_notes = [n for n in s.notes if now - _utc(n["at"]) <= timedelta(days=NOTE_WINDOW_DAYS)]
    if s.represented == "yes" and not (own_visit or s.notes):
        return None

    # (weight, sentence) - the sentence carries the facts; the weight orders them and builds the score.
    since = "since you last reached out " if s.ever_contacted else ""
    bits: List[tuple] = []
    recent = lambda dt: dt is not None and now - _utc(dt) <= timedelta(days=7)

    if s.clicks:
        w = 10 * min(s.clicks, 2) + (5 if recent(s.last_click) else 0)
        txt = (f"Clicked a link in your email {when_phrase(s.last_click, now)}" if s.clicks == 1
               else f"Clicked links in {s.clicks} of your emails {since}- last {when_phrase(s.last_click, now)}")
        bits.append((w, txt))
    if s.emails_opened and s.open_total > s.clicks:
        # (an email they clicked in was opened too - the click line already says that)
        n = max(s.open_total, s.emails_opened)
        w = 7 * min(s.emails_opened, 3) + (5 if n >= 5 else 0) + (8 if recent(s.last_open) else 0) - (7 * min(s.clicks, 3))
        txt = (f"Opened your email {when_phrase(s.last_open, now)}" if n == 1
               else f"Opened your emails {n} times {since}- last {when_phrase(s.last_open, now)}")
        bits.append((max(w, 4), txt))
    showcase_recent = recent(s.last_like) or recent(s.last_comment) or recent(s.last_view)
    sc_bonus = 8 if showcase_recent else 0
    if s.liked:
        homes = _join(s.liked[:2]) if len(s.liked) <= 2 else f"{len(s.liked)} homes"
        bits.append((8 * min(len(s.liked), 3) + sc_bonus, f"Liked {homes} in their Showcase {when_phrase(s.last_like, now)}"))
        sc_bonus = 0
    if s.commented:
        bits.append((10 * min(len(s.commented), 2) + sc_bonus, f"Commented on {_join(s.commented[:2])} in their Showcase {when_phrase(s.last_comment, now)}"))
        sc_bonus = 0
    if s.viewed:
        what = s.viewed[0] if len(s.viewed) == 1 else f"{len(s.viewed)} homes"
        bits.append((2 * min(len(s.viewed), 5) + sc_bonus, f"Looked at {what} in their Showcase - last {when_phrase(s.last_view, now)}"
                     if len(s.viewed) > 1 else f"Looked at {what} in their Showcase {when_phrase(s.last_view, now)}"))
    note = fresh_notes[0] if fresh_notes else (s.notes[0] if s.notes else None)
    if note is not None:
        w = 18 + (6 if now - _utc(note["at"]) <= timedelta(days=30) else 0) + (4 if note.get("own_listing") else 0)
        if not fresh_notes:
            w -= 10
        where = ""
        if note.get("open_house") and note.get("street"):
            where = f" at your {note['street']} open house" if not note.get("own_listing") else f" at your listing, {note['street']}"
        snippet = note_snippet(note["text"])
        more = len(s.notes) - 1
        txt = f"You left a note on {who}{where}: “{snippet}”" + (f" (+{more} more)" if more > 0 else "")
        bits.append((w, txt))
    if len(s.visits) >= 2:
        streets = [v["street"] for v in s.visits if v.get("street")]
        uniq = list(dict.fromkeys(streets))
        bits.append((12, f"Came to {len(s.visits)} of your open houses" + (f" ({_join(uniq[:2])})" if 1 < len(uniq) <= 2 else "")))

    engaged = bool(s.clicks or s.emails_opened or s.liked or s.commented or s.viewed or len(s.visits) >= 2)
    if not engaged and not fresh_notes:
        return None   # no reason - not a blanket check-in
    if s.represented == "yes" and not engaged and not s.notes:
        return None

    tags: List[str] = []
    score = sum(w for w, _ in bits)
    if s.represented == "no":
        score += 25
        tags.append("No agent")
    elif s.represented == "unknown":
        score += 10
    else:
        score -= 15
        tags.append("Has an agent" + (" - your listing" if own_visit else ""))
    for w, label in _intent_bits(s):
        score += w
        if label:
            tags.append(label)
    if s.status == "in_talks":
        score += 6
        tags.append("In talks")
    if s.has_phone:
        score += 4
    if quiet_days <= 30:
        score += 8
    elif quiet_days <= 90:
        score += 3
    elif quiet_days > 180:
        score -= 8
    if not s.ever_contacted:
        score += 4

    # How long since the agent reached out - always the last line.
    first_visit = s.visits[-1] if s.visits else None
    if s.ever_contacted:
        gap = f"Last heard from you {when_phrase(touch, now)} ({gap_phrase(quiet_days)} ago)"
    elif first_visit is not None and first_visit.get("street"):
        gap = f"You haven't reached out since they came to {first_visit['street']} {when_phrase(first_visit['at'], now)}"
    else:
        gap = f"You haven't reached out since they became a lead {when_phrase(created, now)}"

    bits = [b for b in bits if b[1]]
    bits.sort(key=lambda b: -b[0])
    reasons = [t for _, t in bits]
    headline = reasons[0] if reasons else gap
    # Simple = a plain "how's the search going?" fits: nothing personal to work in (no note, no homes
    # they liked / commented on, no agent of their own). Those can be added in one tap, no AI.
    simple = not (s.notes or s.liked or s.commented) and s.represented != "yes"
    return {"score": int(round(score)), "headline": headline, "reasons": reasons[1:4], "tags": tags[:3],
            "gap": gap, "simple": simple, "quiet_days": quiet_days}


def group_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Best first; the top 3 (if good enough) are Recommended, then Worth a check-in, then the rest."""
    items = sorted(items, key=lambda x: (-x["score"], x.get("quiet_days", 0)))
    top = 0
    for it in items:
        if top < TOP_N and it["score"] >= RECOMMENDED_MIN:
            it["group"] = "recommended"
            top += 1
        elif it["score"] >= WORTH_MIN:
            it["group"] = "worth"
        else:
            it["group"] = "less"
    return items


def skipped_until(owner, lead_id: str, now: datetime) -> Optional[datetime]:
    raw = ((getattr(owner, "followup_rules", None) or {}).get(SKIP_KEY) or {}).get(lead_id)
    try:
        until = _utc(datetime.fromisoformat(raw)) if raw else None
    except (TypeError, ValueError):
        return None
    return until if until and until > now else None


def with_skip(rules: Optional[dict], lead_id: str, now: datetime, days: int = SKIP_DAYS) -> dict:
    """followup_rules with this lead skipped for `days` (0 = un-skip); expired skips are dropped."""
    rules = dict(rules or {})
    skips = {}
    for k, v in (rules.get(SKIP_KEY) or {}).items():
        try:
            if _utc(datetime.fromisoformat(v)) > now:
                skips[k] = v
        except (TypeError, ValueError):
            continue
    if days > 0:
        skips[lead_id] = (now + timedelta(days=days)).isoformat()
    else:
        skips.pop(lead_id, None)
    rules[SKIP_KEY] = skips
    return rules


# ------------------------------------------------------------------ reading the signals
_LIKE, _PASS, _VIEW, _COMMENT = "showcase_like", "showcase_dislike", "showcase_view", "property_comment"


def _answer_any(snapshot: Any, *qids: str) -> Optional[str]:
    from app.services.tasks_service import _answer_value
    v = _answer_value(snapshot if isinstance(snapshot, dict) else None, *qids)
    if isinstance(v, list):
        v = ", ".join(str(x) for x in v)
    return str(v) if v not in (None, "") else None


async def gather(db: AsyncSession, owner, lead_id: Optional[str] = None, now: Optional[datetime] = None) -> List[Signals]:
    from app.models.database import AgentTask, ClientEmail, Lead, LeadEvent, LeadNote, OpenHouseEvent
    from app.services.open_house_service import OpenHouseService
    from app.services.tasks_service import _guest_first, lead_alerts
    now = _utc(now) or datetime.now(timezone.utc)
    q = select(Lead).where(Lead.owner_id == owner.id, Lead.hidden_at.is_(None), Lead.status.notin_(DONE_STATUSES),
                           Lead.created_at <= now - timedelta(days=MIN_LEAD_AGE_DAYS))
    if lead_id:
        q = q.where(Lead.id == lead_id)
    leads = [l for l in (await db.execute(q)).scalars().all()
             if lead_alerts(l)["in_app"] and ((l.phone or "").strip() or (l.email or "").strip())
             and not skipped_until(owner, l.id, now)]
    if not leads:
        return []
    ids = [l.id for l in leads]

    open_for, last_done, last_made = set(), {}, {}
    for t in (await db.execute(select(AgentTask.lead_id, AgentTask.completed_at, AgentTask.created_at)
                               .where(AgentTask.owner_id == owner.id, AgentTask.lead_id.in_(ids)))).all():
        if t.completed_at is None:
            open_for.add(t.lead_id)
        else:
            last_done[t.lead_id] = max(_utc(t.completed_at), last_done.get(t.lead_id) or _utc(t.completed_at))
        last_made[t.lead_id] = max(_utc(t.created_at), last_made.get(t.lead_id) or _utc(t.created_at))
    leads = [l for l in leads if l.id not in open_for]
    ids = [l.id for l in leads]
    if not ids:
        return []

    evs: Dict[str, list] = {}
    for e in (await db.execute(select(LeadEvent).where(LeadEvent.lead_id.in_(ids),
                                                       LeadEvent.occurred_at >= now - timedelta(days=EVENT_LOOKBACK_DAYS))
                               .order_by(LeadEvent.occurred_at.desc()))).scalars().all():
        evs.setdefault(e.lead_id, []).append(e)
    notes: Dict[str, list] = {}
    for n in (await db.execute(select(LeadNote).where(LeadNote.lead_id.in_(ids)).order_by(LeadNote.created_at.desc()))).scalars().all():
        if (n.text or "").strip():
            notes.setdefault(n.lead_id, []).append(n)
    oh_ids = {e.open_house_event_id for es in evs.values() for e in es if e.open_house_event_id}
    oh_ids |= {n.open_house_event_id for ns in notes.values() for n in ns if n.open_house_event_id}
    ohs = {o.id: o for o in (await db.execute(select(OpenHouseEvent).where(OpenHouseEvent.id.in_(oh_ids)))).scalars().all()} if oh_ids else {}
    try:
        mine = await OpenHouseService.owned_listing_keys(db, owner, {o.listing_key for o in ohs.values() if o.listing_key})
    except Exception:  # noqa: BLE001
        mine = set()
    own = lambda oh_id: bool(oh_id and oh_id in ohs and ohs[oh_id].listing_key and ohs[oh_id].listing_key in mine)

    by_email = {(l.email or "").strip().lower(): l.id for l in leads if (l.email or "").strip()}
    mails: Dict[str, list] = {}
    if by_email:
        for m in (await db.execute(select(ClientEmail).where(
                ClientEmail.owner_id == owner.id, ClientEmail.recipient_email.in_(list(by_email)),
                ClientEmail.opened_at.is_not(None), ClientEmail.opened_at >= now - timedelta(days=ACTIVITY_WINDOW_DAYS)))).scalars().all():
            lid = by_email.get((m.recipient_email or "").lower())
            if lid:
                mails.setdefault(lid, []).append(m)

    out: List[Signals] = []
    window = now - timedelta(days=ACTIVITY_WINDOW_DAYS)
    for l in leads:
        es = evs.get(l.id, [])
        touch = max([d for d in (_utc(l.last_contacted_at), last_done.get(l.id)) if d], default=None)
        # Anything before the last follow-up was made (or the last touch) is covered already.
        cutoff = max([d for d in (touch, last_made.get(l.id), window) if d])
        s = Signals(lead_id=l.id, name=l.name or l.email or "This lead", first=_guest_first(l.name) or "",
                    represented=l.represented or "unknown", status=l.status or "new",
                    has_phone=bool((l.phone or "").strip()), has_email=bool((l.email or "").strip()),
                    created_at=_utc(l.created_at), last_touch=touch, ever_contacted=touch is not None)
        for n in notes.get(l.id, []):
            oh = ohs.get(n.open_house_event_id) if n.open_house_event_id else None
            s.notes.append({"text": n.text, "at": _utc(n.created_at), "open_house": oh is not None,
                            "street": _street(oh.address) if oh is not None else "", "own_listing": own(n.open_house_event_id)})
        if (l.notes or "").strip():
            s.notes.append({"text": l.notes, "at": _utc(l.updated_at or l.created_at), "open_house": False, "street": "", "own_listing": False})
            s.notes.sort(key=lambda n: n["at"], reverse=True)
        seen_oh = set()
        passed, reacted = set(), set()
        for e in es:   # newest first
            at = _utc(e.occurred_at)
            street = _street(e.property_address)
            if e.source_type == "open_house_visitor":
                key = e.open_house_event_id or street
                if key not in seen_oh:
                    seen_oh.add(key)
                    oh = ohs.get(e.open_house_event_id) if e.open_house_event_id else None
                    s.visits.append({"street": _street(oh.address) if oh is not None else street, "at": at,
                                     "own_listing": own(e.open_house_event_id)})
                if s.timeline is None:
                    s.timeline = _answer_any(e.raw_snapshot, "timeline", "buy_timeline")
                if s.pre_approved is None:
                    s.pre_approved = _answer_any(e.raw_snapshot, "pre_approved")
                continue
            if e.source_type == "form_submission":
                s.timeline = s.timeline or _answer_any(e.raw_snapshot, "timeline", "buy_timeline")
                s.pre_approved = s.pre_approved or _answer_any(e.raw_snapshot, "pre_approved")
            if e.source_type in (_LIKE, _PASS) and street:
                k = street.lower()
                if k not in reacted:
                    reacted.add(k)
                    if e.source_type == _PASS:
                        passed.add(k)
            if at <= cutoff:
                continue
            if e.source_type == _LIKE and street and street.lower() not in passed and street not in s.liked:
                s.liked.append(street)
                s.last_like = s.last_like or at
            elif e.source_type == _COMMENT:
                home = street or "a home"
                if home not in s.commented:
                    s.commented.append(home)
                s.last_comment = s.last_comment or at
            elif e.source_type == _VIEW and street and street.lower() not in passed:
                if street not in s.viewed:
                    s.viewed.append(street)
                s.last_view = s.last_view or at
        s.viewed = [v for v in s.viewed if v not in s.liked]
        for m in mails.get(l.id, []):
            opened = _utc(m.opened_at)
            if opened and opened > cutoff:
                s.emails_opened += 1
                s.open_total += max(1, m.open_count or 0)
                s.last_open = max(opened, s.last_open or opened)
            clicked = _utc(m.clicked_at)
            if clicked and clicked > cutoff:
                s.clicks += 1
                s.last_click = max(clicked, s.last_click or clicked)
        out.append(s)
    return out


async def checkins(db: AsyncSession, owner, lead_id: Optional[str] = None, now: Optional[datetime] = None,
                   limit: int = LIMIT) -> List[Dict[str, Any]]:
    """Every lead worth a check-in, best first, each with its group and reasons."""
    now = _utc(now) or datetime.now(timezone.utc)
    items = []
    for s in await gather(db, owner, lead_id=lead_id, now=now):
        r = evaluate(s, now)
        if r is None:
            continue
        items.append({"lead_id": s.lead_id, "name": s.name, "first": s.first,
                      "kind": "text" if s.has_phone else "email", **r,
                      # the old one-line reason (lead page, follow-up notes)
                      "reason": " · ".join([r["headline"], *r["reasons"][:2]])})
    return group_items(items)[:limit]


def quick_message(first: str, owner, has_showcase: bool) -> str:
    """The one-tap check-in text (no AI) - saved with the {showcase} marker so the link stays current."""
    from app.services.tasks_service import SHOWCASE_MARKER, _guest_first
    me = _guest_first(getattr(owner, "first_name", None)) if owner is not None else ""
    hi = f"Hi {first or 'there'}, it's {me}!" if me else f"Hi {first or 'there'}!"
    if has_showcase:
        return (f"{hi} Just wanted to check in - did any of the homes in your Showcase catch your eye? "
                f"I'd be happy to set up a time for you to tour any of them.\n\n{SHOWCASE_MARKER}")
    return f"{hi} Just wanted to check in and see how your home search is going. Let me know if there's anything I can help with."

