"""The people the chat talks about as small lead cards under the reply (owner, 11/18: "who should I reach
out to?" came back as a wall of text). One boxed card per person - their name, how they came in ("Signed
in at your 1330 N Avignon Dr open house on Oct 11"), whether they have an agent, their lead status and an
Open lead button - then the model's few short points, grouped under its headings ("I'd check in with",
"Less likely").

The model only picks WHO (lead ids) and writes the points; every fact on the card is read here from the
database, never from the model's wording, so it's always right. Same shape as show_homes (ai_home_cards):
a tool the model calls, plus a safety net - leads a lookup returned that the finished reply names get
their cards anyway (one named -> its card is added for free; two or more written out -> one nudge)."""
import re
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.database import Lead, LeadEvent, User

TOOL = {
    "name": "show_leads",
    "description": (
        "Show people from the agent's leads as lead cards under your reply - each card has their name, how they came in, whether "
        "they have an agent, their lead status and an Open lead button (all read from their lead, so never write those yourself), "
        "plus the 1-3 short points you give. ALWAYS use it whenever your reply is about specific leads - who to reach out to or "
        "check in with, what to do today when it's about people, a lead they asked about, guests who signed in. Use the lead_ids "
        "your lookups returned (search_leads, get_lead_detail, get_worth_a_checkin, get_things_to_do, get_priority_summary, "
        "find_open_house_guests...). Group them under short headings in the order to act on them, e.g. \"I'd check in with\" "
        "then \"Less likely\" (one group with no heading is fine). Points: a few words each, why now or what they did "
        "(\"Opened your emails 3 times - last Tuesday\", \"Liked 2 homes in their Showcase\", \"Overdue follow-up: text about "
        "Saturday\") - not their name, source, agent or status, which the card already shows. Then keep your text to one or "
        "two short lines (how many, who to start with) - never write the people out as a list."),
    "input_schema": {"type": "object", "properties": {
        "groups": {"type": "array", "items": {"type": "object", "properties": {
            "heading": {"type": "string"},
            "leads": {"type": "array", "items": {"type": "object", "properties": {
                "lead_id": {"type": "string"},
                "points": {"type": "array", "items": {"type": "string"}}}, "required": ["lead_id"]}}},
            "required": ["leads"]}}},
        "required": ["groups"]},
}
NAME = TOOL["name"]
MAX_LEADS = 10
MAX_GROUPS = 4
MAX_POINTS = 3

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_STATUS = {"new": "New", "contacted": "Contacted", "in_talks": "In talks", "qualified": "In talks",
           "signed": "Signed", "converted": "Signed", "hidden": "Hidden", "archived": "Hidden"}
_AGENT = {"yes": "Has an agent", "no": "No agent"}
# A point that only says what the card already shows ("No agent", "New") is dropped.
_SAME_AS_CARD = re.compile(r"^(?:no agent|has an agent|agent:? not sure|not sure if they have an agent|new|new lead|contacted|in talks|signed)\.?$", re.I)


def _status(lead: Lead) -> Tuple[str, str]:
    """The Leads page's own pipeline step (LeadDetailPanel normalizedStatus + STATUS_LABEL)."""
    raw = "hidden" if lead.hidden_at else (lead.status or "new")
    key = {"qualified": "in_talks", "converted": "signed", "archived": "hidden"}.get(raw, raw)
    return key, _STATUS.get(raw, raw.replace("_", " ").capitalize())


def _short(text: str, limit: int) -> str:
    t = " ".join(str(text or "").split()).lstrip("-•* ").strip()
    if len(t) <= limit:
        return t
    return t[:limit].rsplit(" ", 1)[0].rstrip(",.;:-") + "…"


def _origin(lead: Lead) -> Optional[dict]:
    """Their first real event as the dict checkins_service.origin_line reads - built by the same rules as the
    Dash check-ins (checkins_service.gather's _origin): never an email event, and an event more than 2 days
    after they were added means they were added by hand first."""
    import re as _re
    from datetime import timedelta
    from app.services.checkins_service import NOT_ORIGIN, _street, _utc
    first = next((e for e in reversed(list(lead.events or [])) if e.source_type not in NOT_ORIGIN), None)   # events: newest first
    if first is None:
        return None
    at, created = _utc(first.occurred_at), _utc(lead.created_at)
    if created and at and at - created > timedelta(days=2):
        return None
    snap = first.raw_snapshot if isinstance(first.raw_snapshot, dict) else {}
    oh = getattr(first, "open_house_event", None)
    form = (first.form.title if getattr(first, "form", None) is not None else "") or ""
    if not form:
        m = _re.match(r"^(?:Submitted|Filled out|Started your)\s+(.+?)(?:\s+form)?(?:\s*\(didn't finish\))?$", first.summary or "")
        form = m.group(1) if m else ""
    title = ""
    if first.source_type == "invite_rsvp" and " - " in (first.summary or ""):
        title = first.summary.split(" - ", 1)[1]
    elif first.source_type == "blog_inquiry":
        title = snap.get("blog_title") or ""
    elif first.source_type == "portal_lead":
        title = snap.get("portal_title") or (snap.get("portal") or "").title()
    came = snap.get("came_from") if isinstance(snap.get("came_from"), dict) else {}
    return {"type": first.source_type, "at": at, "street": _street(oh.address if oh is not None else first.property_address),
            "form": form, "title": title, "site": came.get("src") == "site"}


def came_in(lead: Lead, now: Optional[datetime] = None) -> str:
    """How they came in, as one sentence with the date ("Signed in at your 1330 N Avignon Dr open house on
    Oct 11") - the Dash check-ins' own sentence builder (checkins_service.origin_line), so both say the same."""
    from app.services.checkins_service import origin_line
    return origin_line(_origin(lead), lead.created_at, now or datetime.now(timezone.utc))


def card(lead: Lead, points: Optional[List[str]] = None, now: Optional[datetime] = None) -> dict:
    from app.services.checkins_service import nice_name
    status, status_label = _status(lead)
    agent = lead.represented if lead.represented in ("yes", "no") else "unknown"
    return {"lead_id": lead.id, "name": nice_name(lead.name) or lead.email or lead.phone or "A lead",
            "came_in": came_in(lead, now), "agent": agent, "agent_label": _AGENT.get(agent, "Agent: not sure"),
            "status": status, "status_label": status_label,
            "points": [p for p in (_short(x, 110) for x in (points or []) if isinstance(x, str)) if p and not _SAME_AS_CARD.match(p)][:MAX_POINTS],
            "href": f"/leads/{lead.id}"}


async def _load(db: AsyncSession, user: User, refs: List[str]) -> Dict[str, Lead]:
    """The agent's own leads for these refs (an id, or a name the model passed instead) - nobody else's."""
    ids = [r for r in refs if _UUID.fullmatch(r)]
    names = [r for r in refs if not _UUID.fullmatch(r) and len(r) >= 3]
    opts = (selectinload(Lead.events).selectinload(LeadEvent.open_house_event), selectinload(Lead.events).selectinload(LeadEvent.form))
    found: Dict[str, Lead] = {}
    if ids:
        for l in (await db.execute(select(Lead).options(*opts).where(Lead.owner_id == user.id, Lead.id.in_(ids)))).scalars().all():
            found[l.id] = l
    for n in names:
        rows = (await db.execute(select(Lead).options(*opts).where(Lead.owner_id == user.id, Lead.hidden_at.is_(None),
                                                                   Lead.name.ilike(n)).limit(2))).scalars().all()
        if len(rows) == 1:
            found[n] = rows[0]
    return found


async def run(db: AsyncSession, user: User, args: dict) -> Tuple[dict, Optional[dict]]:
    groups_in = args.get("groups")
    if isinstance(groups_in, dict):
        groups_in = [groups_in]
    if not isinstance(groups_in, list):
        # a flat list of leads (no groups) still works
        groups_in = [{"leads": args.get("leads") or []}]
    wanted: List[Tuple[Optional[str], List[dict]]] = []
    for g in groups_in[:MAX_GROUPS]:
        if not isinstance(g, dict):
            continue
        rows = []
        for x in g.get("leads") or []:
            if isinstance(x, str):
                x = {"lead_id": x}
            if isinstance(x, dict) and str(x.get("lead_id") or "").strip():
                rows.append({"ref": str(x["lead_id"]).strip(), "points": x.get("points") if isinstance(x.get("points"), list) else []})
        if rows:
            wanted.append((_short(g.get("heading") or "", 48) or None, rows))
    refs = list(dict.fromkeys(r["ref"] for _, rows in wanted for r in rows))[:MAX_LEADS * 2]
    if not refs:
        return {"error": "no_leads", "message": "Pass the lead_ids from your lookups."}, None
    try:
        found = await _load(db, user, refs)
    except Exception:  # noqa: BLE001 - the reply still works without the cards
        await db.rollback()
        return {"error": "lookup_failed"}, None
    now = datetime.now(timezone.utc)
    groups, used, missing = [], set(), []
    for heading, rows in wanted:
        cards = []
        for r in rows:
            if len(used) >= MAX_LEADS:
                break
            lead = found.get(r["ref"])
            if lead is None:
                if r["ref"] in refs:   # (past the cap isn't "not found")
                    missing.append(r["ref"])
                continue
            if lead.id in used:
                continue
            used.add(lead.id)
            cards.append(card(lead, r["points"], now))
        if cards:
            groups.append({"heading": heading, "leads": cards})
    if not groups:
        return {"error": "not_found", "message": "None of those are this agent's leads - use the lead_id values your lookups returned.",
                "not_found": missing[:5]}, None
    entry = {"tool": NAME, "record_type": "leads", "record_id": str(uuid.uuid4()), "groups": groups}
    shown = [c for g in groups for c in g["leads"]]
    result = {"shown": len(shown),
              "cards": [f"{c['name']} - {c['came_in']}; {c['agent_label']}; {c['status_label']}" for c in shown],
              "note": ("They now see a card for each person under your reply: name, how they came in, agent, lead status, your points "
                       "and an Open lead button. Don't repeat any of that - not even the points - or list the people, and never mention "
                       "the cards. For several people reply in one or two short lines: who to start with and why in a few words "
                       "(\"3 people worth a text - start with Jane Smith: most active and no agent.\"), "
                       "at most two names; for one person, answer what they asked in a few short lines. Never 'he' or 'she' - their name or 'they'.")}
    if missing:
        result["not_found"] = missing[:5]
    return result, entry


def lead_names(entry: dict) -> List[Tuple[str, str]]:
    """(name, lead_id) for every card on a reply - the next message is told them (ai_routes._ids_note)."""
    return [(c.get("name") or "", c.get("lead_id") or "") for g in entry.get("groups") or [] for c in g.get("leads") or []]


# ------------------------------------------------------------------ safety net
_NAME_KEYS = ("name", "lead_name", "visitor_name", "guest_name", "client_name", "who")
_WORD = re.compile(r"[^a-z0-9' ]+")


def _norm(s: str) -> str:
    return " ".join(_WORD.sub(" ", re.sub(r"\([^)]*\)", " ", str(s or "")).lower()).split())   # "Jane Hotlead (Test)" -> "jane hotlead"


def collect(result, seen: dict, depth: int = 0) -> None:
    """Remember every lead (lead_id -> name) a lookup result carries."""
    if depth > 5 or len(seen) > 300:
        return
    if isinstance(result, dict):
        lid = result.get("lead_id")
        if not lid and result.get("id") and ("represented" in result or "primary_source" in result):
            lid = result.get("id")   # search_leads / get_lead_detail rows
        if not lid:   # get_priority_summary / Rundown items: "/leads/<id>" links, "lead-<id>-kind" ids
            link, iid = str(result.get("link") or result.get("lead_page") or ""), str(result.get("id") or "")
            m = re.fullmatch(r"/leads/([0-9a-fA-F-]{36})", link) or (_UUID.search(iid) if iid.startswith("lead-") else None)
            lid = (m.group(1) if m.re.groups else m.group(0)) if m else None
        name = next((result.get(k) for k in _NAME_KEYS if isinstance(result.get(k), str) and result.get(k).strip()), None)
        if lid and name and _UUID.fullmatch(str(lid)):
            n = _norm(name)
            if len(n.split()) >= 2:
                seen.setdefault(str(lid), n)
        for v in result.values():
            if isinstance(v, (dict, list)):
                collect(v, seen, depth + 1)
    elif isinstance(result, list):
        for v in result[:200]:
            collect(v, seen, depth + 1)


def mentioned(text: str, seen: dict) -> List[str]:
    """The lead ids whose full name the reply says, in the order it says them."""
    low = " " + _norm(text) + " "
    hits = []
    for lid, name in seen.items():
        i = low.find(" " + name + " ")
        if i >= 0:
            hits.append((i, lid))
    return list(dict.fromkeys(lid for _, lid in sorted(hits)))


# "Status: New" / "Source: Open house sign-in" / "Agent status: Unknown" lines next to a card that shows them (11/18 test)
_RESTATED = re.compile(r"^\s*[-*•]?\s*\**\s*(?:lead status|status|source|came in|how they came in|agent(?: status)?|has an agent)\s*\**\s*:", re.I | re.M)


def restates_card(text: str) -> bool:
    return len(_RESTATED.findall(text or "")) >= 2


NUDGE = ("(App note: you wrote people from their leads out in text. Call show_leads with their lead_ids - grouped in the order to act "
         "(e.g. \"I'd check in with\", \"Less likely\"), 1-3 short points each on why now - then reply in one or two short lines. Never "
         "list the people, how they came in, their agent or their status in text: the cards show all that.)")

SHORTEN = ("(App note: those people already show as lead cards under your reply (name, how they came in, agent, status, your points, "
           "Open lead). Write your reply again in one or two short lines - who to start with and why in a few words, at most two "
           "people named - with no list. Don't call show_leads again.)")

SHORTEN_ONE = ("(App note: the lead card under your reply already shows their name, how they came in (with the open house or form and "
               "the date), whether they have an agent and their status. Write your reply again without any of that - only what matters "
               "beyond the card, in a few short lines, by name or 'they'. Don't call show_leads again.)")
