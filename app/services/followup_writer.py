"""Write it for me (owner, 11/09): the text an agent would send THIS lead, written by the AI from what we actually
know about them - which open house (and how long ago), what they answered signing in, the agent's notes, their
Showcase (opened or not, liked, passed), what the agent already sent (never pitch the Showcase twice) and when
they last heard from the agent.

- One small AI call, on the agent's tap only (never in the background); costs a little of their AI usage,
  checked first and recorded like any chat answer.
- Time stays right: the message never says "yesterday" / "this weekend" itself - it says when the open house was
  as a slot, {oh_when:YYYY-MM-DD}, filled in the day the message is shown (tasks_service.fill_when). A follow-up
  that rolls over as overdue reads right the next day, for free.
- No made-up facts: only what's below goes in; the Showcase link is added by the app, last, never by the AI.

Also: which leads should have a check-in waiting (checkins -> checkins_service) - no AI, just the reasons."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import AgentTask, Lead, LeadEvent, LeadNote, OpenHouseEvent, User

logger = get_logger(__name__)

_EST_IN, _EST_OUT = 2600, 260   # tokens a write takes (for the cost shown before the tap)
_PITCH_SOURCES = {"open_house_signin", "similar_homes_qr", "showcase_setup", "buyer_form", "how_can_we_help"}
_SHOWCASE_SEEN = {"showcase_view", "showcase_like", "showcase_dislike", "email_clicked", "property_comment"}

_SYSTEM = (
    "You write ONE follow-up message a real-estate agent will send to one lead, in the agent's own voice (first person). "
    "It must read like the agent typed it themselves for this person today: warm, plain, specific to their situation, "
    "never salesy, no emojis, no hashtags, 2-4 short sentences (under 300 characters for a text).\n"
    "Rules:\n"
    "- Use ONLY the facts given. Never invent homes, prices, features, dates, plans or things the agent did.\n"
    "- Start like a text from the agent: 'Hi {first}, it's {agent}!' (use the names given). No sign-off - it is added.\n"
    "- Timing of the open house: never write day or date words yourself (no 'yesterday', 'this weekend', 'Sunday'). "
    "Where you mention when the open house was, write the token {when} at the end of that clause, e.g. "
    "'thanks again for coming to my open house at 239 Windham Dr {when}'. Only use {when} if an open house is given; "
    "when one is given and you mention it, always add {when}.\n"
    "- If showcase.must_haves is given: never call their homes 'similar to the home you toured' or 'like it' - they "
    "are 'homes that might interest you'. If you talk about the homes, say how many have the must-haves in plain "
    "words (showcase.must_have_sentence), and if you name a home, name only showcase.home_to_name_first.\n"
    "- If the Showcase pitch was already sent, do NOT explain or pitch the Showcase again. If they never opened it, "
    "a natural move is asking if they're still looking and offering to send homes now and then.\n"
    "- If they liked or passed on homes, you may refer to that in general words (no addresses unless given as liked).\n"
    "- Say what they want in the same terms as the notes / answers - never reword it into something different (a "
    "'first-floor primary' is NOT 'single-level'; 'near the train' is NOT 'walkable').\n"
    "- Use the agent's notes to make it personal (their timeline, what they want), but never quote a note word for word "
    "and never mention anything private or about who they are (family, age, religion, origin, disability) - homes, "
    "timing and their search only.\n"
    "- If the lead has their own agent: only talk about the agent's own listing they saw, and write it so it works if "
    "they pass it to their agent (e.g. 'if you or your agent have any questions about ...'). Never offer to help them search.\n"
    "- If the agent gave a goal for this follow-up, write toward it.\n"
    "- link: true only if adding their Showcase link at the end would help (they have one and it fits); the app adds it.\n"
    "Answer with JSON only: {\"title\":\"short to-do for the agent, e.g. 'Text Nicole - still looking?'\","
    "\"message\":\"...\",\"link\":true|false,\"why\":\"at most 10 words for the agent: why this message\"}"
)


def _first_name(name: Optional[str]) -> str:
    from app.services.tasks_service import _guest_first
    return _guest_first(name) or ""


def _eastern_date(dt: Optional[datetime]):
    from app.services.tasks_service import EASTERN
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(EASTERN).date()


def _street(address: Optional[str]) -> str:
    from app.services.email_service import tidy_caps
    s = (address or "").split(",")[0].strip()
    return tidy_caps(s) if s else ""


def _answers(raw: Any) -> List[str]:
    """Sign-in / form answers as 'Question: answer' lines (whatever shape they were saved in)."""
    out: List[str] = []
    if isinstance(raw, dict):
        raw = raw.get("answers", raw)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if v not in (None, "", [], {}):
                out.append(f"{k}: {v}")
    elif isinstance(raw, list):
        for a in raw:
            if isinstance(a, dict):
                q = a.get("label") or a.get("question") or a.get("q") or a.get("key")
                v = a.get("answer") or a.get("value") or a.get("a")
                if q and v not in (None, "", []):
                    out.append(f"{q}: {v}")
    return [x[:160] for x in out][:12]


async def facts(db: AsyncSession, owner: User, lead: Lead) -> Dict[str, Any]:
    """Everything the message may draw on - real data only."""
    from app.services import tasks_service as ts
    now = datetime.now(timezone.utc)
    events = (await db.execute(select(LeadEvent).where(LeadEvent.lead_id == lead.id)
                               .order_by(LeadEvent.occurred_at.desc()).limit(40))).scalars().all()
    visit = next((e for e in events if e.source_type == "open_house_visitor"), None)
    oh, own_listing = None, False
    if visit is not None and visit.open_house_event_id:
        oh = await db.get(OpenHouseEvent, visit.open_house_event_id)
        if oh is not None and oh.listing_key:
            try:
                from app.services.open_house_service import OpenHouseService
                own_listing = oh.listing_key in await OpenHouseService.owned_listing_keys(db, owner, {oh.listing_key})
            except Exception:  # noqa: BLE001
                own_listing = False
    notes = (await db.execute(select(LeadNote).where(LeadNote.lead_id == lead.id).order_by(LeadNote.created_at.desc()).limit(8))).scalars().all()
    note_lines = [("From the open house: " if n.open_house_event_id else "") + " ".join((n.text or "").split())[:300] for n in notes if (n.text or "").strip()]
    if (lead.notes or "").strip():
        note_lines.append(" ".join(lead.notes.split())[:400])
    tasks = (await db.execute(select(AgentTask).where(AgentTask.lead_id == lead.id).order_by(AgentTask.created_at.desc()).limit(12))).scalars().all()
    pitch_sent = any(t.auto_source in _PITCH_SOURCES and t.completed_at is not None for t in tasks)
    sent = []
    for t in tasks:
        if t.completed_at is None:
            continue
        msg = " ".join(ts.fill_when(t.suggested_message or "").split())
        sent.append({"done": str(_eastern_date(t.completed_at)), "how": t.kind, "title": t.title,
                     "message": re.sub(r"https?://\S+", "[link]", msg)[:260]})
    showcase = None
    try:
        info = (await ts.lead_showcase_info(db, owner, [lead])).get(lead.id)
    except Exception:  # noqa: BLE001
        info = None
    if info:
        likes = sum(1 for e in events if e.source_type == "showcase_like")
        passes = sum(1 for e in events if e.source_type == "showcase_dislike")
        showcase = {"homes": info.get("count") or 0, "opened": any(e.source_type in _SHOWCASE_SEEN for e in events),
                    "liked": likes, "passed": passes, "pitch_already_sent": pitch_sent,
                    "must_haves_matching": (info.get("smart") or {}).get("n") or 0}
        smart, parts = info.get("smart") or {}, info.get("parts") or {}
        if smart.get("wants") and parts.get("must_line"):
            # What they want now (the Showcase's must-haves) - the same words and home the template text uses.
            showcase["must_haves"] = [str(w) for w in smart["wants"]]
            showcase["must_have_sentence"] = parts["must_line"]
            if parts.get("best_home"):
                showcase["home_to_name_first"] = parts["best_home"] + (f" - {parts['why_now']}" if parts.get("why_now") else "")
    last = lead.last_contacted_at
    return {
        "today": f"{_eastern_date(now)} ({_eastern_date(now).strftime('%A')})",
        "first": _first_name(lead.name), "agent": (owner.first_name or "").strip(), "brokerage": (owner.brokerage or "").strip(),
        "has_own_agent": lead.represented == "yes", "status": lead.status,
        "has_phone": bool(lead.phone), "has_email": bool(lead.email),
        "met_at_open_house": ({"address": _street(oh.address if oh is not None else visit.property_address),
                               "date": str(_eastern_date(visit.occurred_at)), "agents_own_listing": own_listing}
                              if visit is not None else None),
        "signed_in_answers": _answers(visit.raw_snapshot) if visit is not None else [],
        "notes": note_lines[:6],
        "showcase": showcase,
        "last_heard_from_agent": str(_eastern_date(last)) if last else "never",
        "messages_already_sent": sent[:4],
        "recent_activity": [f"{_eastern_date(e.occurred_at)}: {e.summary}" for e in events[:12]],
        "lead_since": str(_eastern_date(lead.created_at)),
        "_oh_date": str(_eastern_date(visit.occurred_at)) if visit is not None else None,
        "_showcase_url": (info or {}).get("url"),
    }


async def estimate(db: AsyncSession, owner: User) -> Dict[str, Any]:
    """What one write takes of the agent's AI usage - shown on the button before the tap."""
    from app.services.ai import ai_home_reader as reader
    from app.services.ai.ai_config_service import ai_config_service
    config = await ai_config_service.get_config(db)
    est = ai_config_service.estimate_cost_usd(config, config["model"], _EST_IN, _EST_OUT)
    left, limit = await reader.room(db, owner, config)
    return {"usage_percent": reader.percent(est, limit) if limit else 0.0,
            "left_percent": None if left is None else reader.percent(left, limit),
            "enough": left is None or est <= left}


def _clean_message(msg: str, oh_date: Optional[str], smart: bool = False) -> str:
    from app.services import tasks_service as ts
    msg = " ".join(str(msg or "").replace("\r", "").split(" "))
    msg = re.sub(r"https?://\S+", "", msg)                      # links are the app's job
    msg = msg.replace("{when}", "{oh_when:%s}" % oh_date if oh_date else "recently")
    msg = re.sub(r"\{(?!oh_when:\d{4}-\d{2}-\d{2}\})[^{}]*\}", "", msg)   # any other stray {token}
    msg = re.sub(r"[ \t]{2,}", " ", msg).strip()
    if oh_date:
        # Mentioned the open house but left out when: the slot goes in anyway (owner, 11/09).
        try:
            from datetime import date as _date
            msg = ts.with_when_slot(msg, _date.fromisoformat(oh_date)) or msg
        except ValueError:
            pass
    if smart:
        # Must-haves known: never "homes similar to the one you toured".
        msg = ts._SIMILAR_TO_TOURED.sub(ts.SMART_INSTEAD_OF_SIMILAR, msg)
    return msg[:700]


async def write(db: AsyncSession, owner: User, lead: Lead, goal: Optional[str] = None) -> Dict[str, Any]:
    """{message (with {oh_when:..} slots, no footer, {showcase} marker when the link fits), preview (as the client
    would get it today), title, why, used_percent} - or {error} when the AI usage has no room / it failed."""
    from app.services import tasks_service as ts
    from app.services.ai import ai_home_reader as reader
    from app.services.ai.ai_config_service import ai_config_service
    from app.services.ai.ai_rate_limit_service import AIRateLimitExceeded, record_usage_event, reconcile_budget, reserve_global_and_emergency
    config = await ai_config_service.get_config(db)
    est = ai_config_service.estimate_cost_usd(config, config["model"], _EST_IN, _EST_OUT)
    left, limit = await reader.room(db, owner, config)
    if left is not None and est > left:
        return {"error": "Not enough AI usage left this month to write it - it resets on your billing day."}
    f = await facts(db, owner, lead)
    payload = {k: v for k, v in f.items() if not k.startswith("_")}
    if goal and goal.strip():
        payload["agent_goal_for_this_follow_up"] = goal.strip()[:200]
    try:
        reservation = await reserve_global_and_emergency(db, "chat_global", max(est, 0.005), config["chat_global_daily_cost_ceiling_usd"],
                                                         config["chat_global_monthly_cost_ceiling_usd"], config)
    except AIRateLimitExceeded:
        return {"error": "Writing is paused for a moment - try again shortly."}
    spent = 0.0
    try:
        resp = await asyncio.wait_for(reader._get_client().messages.create(
            model=config["model"], max_tokens=500, system=_SYSTEM,
            messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
            **ai_config_service.model_options(config["model"], "low")), timeout=25)
        spent = ai_config_service.estimate_cost_usd(config, config["model"], resp.usage.input_tokens, resp.usage.output_tokens)
        await record_usage_event(db, owner.id, None, config["model"], resp.usage.input_tokens, resp.usage.output_tokens, spent, 0, False, None,
                                 tool_names=["followup_writer"])
        text_out = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        m = re.search(r"\{.*\}", text_out, re.S)
        out = json.loads(m.group(0)) if m else {}
    except Exception:  # noqa: BLE001
        logger.warning("follow-up writer failed for lead %s", lead.id, exc_info=True)
        return {"error": "Couldn't write it just now - try again."}
    finally:
        await reconcile_budget(db, reservation, spent)
    message = _clean_message(out.get("message") or "", f["_oh_date"], smart=bool((f.get("showcase") or {}).get("must_haves")))
    if len(message) < 12:
        return {"error": "Couldn't write it just now - try again."}
    if out.get("link") and f["_showcase_url"]:
        message = f"{message}\n\n{ts.SHOWCASE_MARKER}"
    title = " ".join(str(out.get("title") or "").split())[:80] or f"Check in with {f['first'] or 'them'}"
    # As the client would get it today: time words filled, signed, their Showcase link last.
    preview = ts.fill_when(message).replace(ts.SHOWCASE_MARKER, "").strip()
    preview = ts.with_footer(preview, owner) or preview
    if ts.SHOWCASE_MARKER in message and f["_showcase_url"]:
        preview = f"{preview}\n\n{f['_showcase_url']}"
    return {"message": message, "preview": preview, "title": title,
            "text": ts.fill_when(message).replace(ts.SHOWCASE_MARKER, "").strip(), "link": ts.SHOWCASE_MARKER in message, "why": " ".join(str(out.get("why") or "").split())[:90],
            "kind": "text" if lead.phone else "email",
            "used_percent": reader.percent(spent, limit) if limit else None}


async def checkins(db: AsyncSession, owner: User, lead_id: Optional[str] = None, limit: int = 60) -> List[Dict[str, Any]]:
    """Leads worth a check-in, best first, with their group and reasons - see checkins_service (owner, 11/13)."""
    from app.services import checkins_service
    return await checkins_service.checkins(db, owner, lead_id=lead_id, limit=limit)
