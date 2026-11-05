"""The AI helper's actions (10/30) - each a `prepare` (checks the request against the agent's own
data and writes the Confirm card; changes nothing) and a `run` (only after the agent taps
Confirm). Every run calls the SAME route the page's own button calls, as the agent, through
ai_route_bridge - so plan, broker-authorization, ownership and validation rules are the app's own.

Rules every action here follows (see ai_actions.py): no deletes, no billing, nothing paid without
saying so, and anything that emails or notifies a real person says so on the card. Paid AI from
the chat says so on its card ("This uses your monthly AI allowance") - the AI CMA and the AI blog
writer (11/01) and AI media (11/05, ai_actions_media.py). Left out on
purpose: publishing the IDX website draft (no undo), connecting calendars/mailboxes (needs the agent's own
sign-in), domains, reviews imports (the agent must attest they're real).
"""
import re
from datetime import date as _date, datetime
from typing import Any, Callable, Dict, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import (
    AgentReview, AgentTask, Collection, CollectionPreferences, Lead, OpenHouseEvent, OpenHouseFormRequest, OpenHouseHostRequest,
    OpenHouseVisitor, Property, PropertyTour, PropertyWebsite, SupportTicket, User,
)
from app.models.database import AIConversation, AIMessage
from app.services.ai.ai_actions import ActionError, Prepared, register
from app.services.email_service import tidy_caps
from app.config.logging import get_logger

logger = get_logger(__name__)
from app.services.ai.ai_route_bridge import RouteError, call_as_agent

MLS = {"BASIC", "PREMIUM", "IDX"}
SHOWCASE = {"PREMIUM", "IDX"}
IDX = {"IDX"}
REVIEWS = {"PREMIUM", "IDX", "PRO_NO_MLS"}
STUDIO = {"PREMIUM", "IDX", "PRO_NO_MLS"}

_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# ---------------------------------------------------------------- small helpers

def _s(v: Any) -> str:
    return str(v or "").strip()


def _need_id(args: dict, key: str) -> str:
    v = _s(args.get(key))
    if not _ID.match(v):
        raise ActionError(f"I need to look that up first ({key.replace('_', ' ')} missing).")
    return v


def _obj(props: dict, required=()) -> dict:
    return {"type": "object", "properties": props, **({"required": list(required)} if required else {})}


def _date_ok(v: str) -> str:
    try:
        return _date.fromisoformat(_s(v)).isoformat()
    except ValueError:
        raise ActionError("I need the date as YYYY-MM-DD.")


def _time_ok(v: str) -> str:
    v = _s(v)[:5]
    if not _TIME.match(v):
        raise ActionError("I need the time as HH:MM (24-hour).")
    return v


def _nice_time(v: str) -> str:
    try:
        return datetime.strptime(v[:5], "%H:%M").strftime("%-I:%M %p")
    except ValueError:
        try:
            return datetime.strptime(v[:5], "%H:%M").strftime("%I:%M %p").lstrip("0")
        except ValueError:
            return v


def _nice_date(v: str) -> str:
    try:
        d = _date.fromisoformat(v[:10])
        return f"{d:%a, %b} {d.day}"
    except ValueError:
        return v


def _yn(v: bool) -> str:
    return "On" if v else "Off"


async def _run_route(user: User, method: str, path: str, body: Any = None) -> Any:
    try:
        return await call_as_agent(user, method, path, json=body)
    except RouteError as e:
        raise ActionError(str(e))


async def _one(db: AsyncSession, stmt, missing: str):
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise ActionError(missing)
    return row


async def _lead(db, user, args) -> Lead:
    return await _one(db, select(Lead).where(Lead.id == _need_id(args, "lead_id"), Lead.owner_id == user.id), "I couldn't find that person in your leads.")


async def _task(db, user, args) -> AgentTask:
    return await _one(db, select(AgentTask).where(AgentTask.id == _need_id(args, "task_id"), AgentTask.owner_id == user.id), "I couldn't find that follow-up.")


async def _oh(db, user, args) -> OpenHouseEvent:
    return await _one(db, select(OpenHouseEvent).where(OpenHouseEvent.id == _need_id(args, "open_house_id"), OpenHouseEvent.agent_id == user.id,
                                                       OpenHouseEvent.is_deleted.is_(False)), "I couldn't find that open house.")


async def _showcase(db, user, args) -> Collection:
    return await _one(db, select(Collection).where(Collection.id == _need_id(args, "collection_id"), Collection.owner_id == user.id), "I couldn't find that Showcase.")


def _oh_name(oh: OpenHouseEvent) -> str:
    return oh.abbreviated_address or oh.address or "your open house"


def _sc_name(c: Collection) -> str:
    return f"{c.visitor_name}'s Showcase" if c.visitor_name else (c.name or "this Showcase")


def _simple(name: str, description: str, schema: dict, prepare, run, plans=None, adjust=None):
    register({"name": name, "description": description, "input_schema": schema}, prepare, run, plans, adjust=adjust)


# ================================================================ LEADS

LEAD_STATUS = {"new": "New", "contacted": "Contacted", "in_talks": "In talks", "signed": "Signed"}


async def _p_update_lead(db, user, a):
    lead = await _lead(db, user, a)
    body, lines = {}, []
    if a.get("status"):
        if a["status"] not in LEAD_STATUS:
            raise ActionError("Status can be New, Contacted, In talks or Signed.")
        body["status"] = a["status"]; lines.append(f"Status: {LEAD_STATUS[a['status']]}")
    if a.get("mark_contacted"):
        body["mark_contacted"] = True; lines.append("Mark as contacted today")
    if a.get("has_agent") in ("yes", "no", "unknown"):
        body["represented"] = a["has_agent"]
        lines.append({"yes": "Has an agent (moves them to the Has agent tab and stops most automatic follow-ups)", "no": "No agent", "unknown": "Not sure if they have an agent"}[a["has_agent"]])
    if isinstance(a.get("starred"), bool):
        body["is_favorite"] = a["starred"]; lines.append("Star them" if a["starred"] else "Remove the star")
    tags = list(lead.tags or [])
    add = [t.strip() for t in (a.get("add_tags") or []) if _s(t)]
    rem = {t.strip().lower() for t in (a.get("remove_tags") or []) if _s(t)}
    if add or rem:
        new = [t for t in tags if t.lower() not in rem] + [t for t in add if t.lower() not in {x.lower() for x in tags}]
        body["tags"] = new
        if add: lines.append("Add tags: " + ", ".join(add))
        if rem: lines.append("Remove tags: " + ", ".join(sorted(rem)))
    for k, label in (("name", "Name"), ("email", "Email"), ("phone", "Phone")):
        if _s(a.get(k)):
            body[k] = _s(a[k]); lines.append(f"{label}: {body[k]}")
    if not body:
        raise ActionError("Tell me what to change about them.")
    warn = "Changing their email restarts their automatic emails at the new address." if "email" in body else None
    return Prepared(f"Update {lead.name or 'this lead'}", lines, {"lead_id": lead.id, "body": body}, warning=warn)


async def _r_update_lead(db, user, p):
    await _run_route(user, "PATCH", f"/leads/{p['lead_id']}", p["body"])
    return {"message": "Updated.", "link": f"/leads/{p['lead_id']}"}

_simple("update_lead", "Change a lead: pipeline status, mark contacted, whether they have an agent, star, tags, or their name/email/phone. Use lead_id from search_leads.",
        _obj({"lead_id": {"type": "string"}, "status": {"type": "string", "enum": list(LEAD_STATUS)}, "mark_contacted": {"type": "boolean"},
              "has_agent": {"type": "string", "enum": ["yes", "no", "unknown"]}, "starred": {"type": "boolean"},
              "add_tags": {"type": "array", "items": {"type": "string"}}, "remove_tags": {"type": "array", "items": {"type": "string"}},
              "name": {"type": "string"}, "email": {"type": "string"}, "phone": {"type": "string"}}, ["lead_id"]),
        _p_update_lead, _r_update_lead)


async def _p_lead_note(db, user, a):
    lead = await _lead(db, user, a)
    text = _s(a.get("text"))
    if not text:
        raise ActionError("What should the note say?")
    return Prepared(f"Add a note to {lead.name or 'this lead'}", [f"“{text[:400]}”"], {"lead_id": lead.id, "text": text})


async def _r_lead_note(db, user, p):
    await _run_route(user, "POST", f"/leads/{p['lead_id']}/notes", {"text": p["text"]})
    return {"message": "Note added.", "link": f"/leads/{p['lead_id']}"}

_simple("add_lead_note", "Add a private note to a lead (shows on their lead page and follow-ups).",
        _obj({"lead_id": {"type": "string"}, "text": {"type": "string"}}, ["lead_id", "text"]), _p_lead_note, _r_lead_note)


async def _p_lead_alerts(db, user, a):
    lead = await _lead(db, user, a)
    body, lines = {}, []
    if isinstance(a.get("calendar"), bool):
        body["calendar"] = a["calendar"]; lines.append(f"{lead.name}'s follow-ups on your calendar: {_yn(a['calendar'])}")
        if a["calendar"]:
            body["in_app"] = True
    if isinstance(a.get("keep_followups_in_app"), bool) and not body.get("calendar"):
        body["in_app"] = a["keep_followups_in_app"]
        lines.append("Keep their follow-ups in OpenHousePal" if a["keep_followups_in_app"] else "Stop making follow-ups for them")
    if isinstance(a.get("email_on_likes"), bool):
        body["emails"] = a["email_on_likes"]; lines.append(f"Email you when they like or comment: {_yn(a['email_on_likes'])}")
    if not body:
        raise ActionError("Which alert should change?")
    return Prepared(f"{lead.name}'s alerts", lines, {"lead_id": lead.id, "body": body})


async def _r_lead_alerts(db, user, p):
    await _run_route(user, "PUT", f"/leads/{p['lead_id']}/alerts", p["body"])
    return {"message": "Alerts saved.", "link": f"/leads/{p['lead_id']}"}

_simple("set_lead_alerts", "A lead's own alert switches: their follow-ups on the agent's calendar; when calendar is off, keep follow-ups in OpenHousePal or stop them; email the agent when they like/comment in their Showcase.",
        _obj({"lead_id": {"type": "string"}, "calendar": {"type": "boolean"}, "keep_followups_in_app": {"type": "boolean"}, "email_on_likes": {"type": "boolean"}}, ["lead_id"]),
        _p_lead_alerts, _r_lead_alerts)


async def _p_hide_lead(db, user, a):
    lead = await _lead(db, user, a)
    return Prepared(f"Hide {lead.name or 'this lead'}", ["Moves them out of your Leads list (you can unhide them later)"], {"lead_id": lead.id},
                    warning="Hiding also clears their open follow-ups, and unhiding won't bring those back.")


async def _r_hide_lead(db, user, p):
    await _run_route(user, "PATCH", f"/leads/{p['lead_id']}/hide")
    return {"message": "Hidden.", "link": "/leads"}

_simple("hide_lead", "Hide a lead from the Leads list (not a delete).", _obj({"lead_id": {"type": "string"}}, ["lead_id"]), _p_hide_lead, _r_hide_lead)

HANDLED = {"disclosure": "seller disclosure request", "second_visit": "second visit request", "showing": "showing request",
           "comment": "Showcase comments", "likes": "Showcase likes", "refine": "request to change their search"}


async def _p_handled(db, user, a):
    lead = await _lead(db, user, a)
    kind = _s(a.get("kind"))
    if kind not in HANDLED:
        raise ActionError("Which request is done?")
    # Only when one is actually waiting - the card said "Marked done" for a request that wasn't there (11/02)
    detail = await _run_route(user, "GET", f"/leads/{lead.id}")
    if kind not in {t.get("kind") for t in (detail or {}).get("todos") or []}:
        raise ActionError(f"{lead.name} has no {HANDLED[kind]} waiting on you, so there's nothing to clear.")
    return Prepared(f"Mark {lead.name}'s {HANDLED[kind]} as done", ["Clears it from Waiting on you", "Doesn't message them"], {"lead_id": lead.id, "kind": kind})


async def _r_handled(db, user, p):
    await _run_route(user, "POST", f"/leads/{p['lead_id']}/handled", {"kind": p["kind"]})
    return {"message": "Marked done.", "link": f"/leads/{p['lead_id']}"}

_simple("mark_lead_request_done", "Mark something a lead asked for as handled (clears it from 'Waiting on you'): disclosure, second_visit, showing, comment, likes, refine.",
        _obj({"lead_id": {"type": "string"}, "kind": {"type": "string", "enum": list(HANDLED)}}, ["lead_id", "kind"]), _p_handled, _r_handled)


async def _p_reinvite(db, user, a):
    lead = await _lead(db, user, a)
    kind = "open_house" if a.get("kind") == "open_house" else "showcase"
    opts = await _run_route(user, "GET", f"/leads/{lead.id}/invite-options")
    if kind == "open_house" and not (opts or {}).get("open_house"):
        kind = "showcase"
    if not (opts or {}).get("collection_id"):
        raise ActionError(f"{lead.name} doesn't have a live Showcase to send.")
    return Prepared(f"Resend {lead.name} their Showcase invite", [f"To: {lead.email}"], {"lead_id": lead.id, "kind": kind},
                    warning=f"This emails {lead.name} right away.", confirm_label="Send it")


async def _r_reinvite(db, user, p):
    await _run_route(user, "POST", f"/leads/{p['lead_id']}/reinvite", {"kind": p["kind"]})
    return {"message": "Sent.", "link": f"/leads/{p['lead_id']}"}

_simple("resend_showcase_invite", "Email a lead their Showcase invite again (Premium/IDX).", _obj({"lead_id": {"type": "string"}, "kind": {"type": "string", "enum": ["showcase", "open_house"]}}, ["lead_id"]),
        _p_reinvite, _r_reinvite, SHOWCASE)


async def _p_client_emails(db, user, a):
    email = _s(a.get("email")).lower()
    if "@" not in email:
        raise ActionError("Which client's email address?")
    on = bool(a.get("on"))
    # Their name when they're one of the agent's leads - the address alone read like a stranger (11/02 review)
    name = (await db.execute(select(Lead.name).where(Lead.owner_id == user.id, func.lower(Lead.email) == email, Lead.name.isnot(None)).limit(1))).scalar()
    return Prepared(f"{'Turn back on' if on else 'Turn off'} automatic emails to {name or email}",
                    ([email] if name else []) + ["Showcase updates and other automatic emails to this client" + (" start again" if on else " stop until you turn them back on")],
                    {"email": email, "on": on})


async def _r_client_emails(db, user, p):
    await _run_route(user, "PUT", "/client-emails/people", {"email": p["email"], "on": p["on"]})
    return {"message": "Saved.", "link": "/leads/emails"}

_simple("set_client_automatic_emails", "Email Tracker: turn a client's automatic emails off, or back on.", _obj({"email": {"type": "string"}, "on": {"type": "boolean"}}, ["email", "on"]),
        _p_client_emails, _r_client_emails)


# ================================================================ FOLLOW-UPS

def _when(a: dict) -> Optional[str]:
    if not _s(a.get("due_date")):
        return None
    d = _date_ok(a["due_date"])
    t = _time_ok(a["due_time"]) if _s(a.get("due_time")) else "12:00"
    # The agent means Eastern (like the page, which sends a zoned time) - never a bare time the
    # server would read as UTC (10 AM would land at 6 AM).
    from zoneinfo import ZoneInfo
    return datetime.fromisoformat(f"{d}T{t}:00").replace(tzinfo=ZoneInfo("America/New_York")).isoformat()


async def _p_new_task(db, user, a):
    title = _s(a.get("title"))
    if not title:
        raise ActionError("What's the follow-up?")
    body = {"title": title[:200], "kind": a.get("kind") if a.get("kind") in ("call", "text", "email", "showing", "general") else "general"}
    lines = [title]
    if a.get("lead_id"):
        lead = await _lead(db, user, a); body["lead_id"] = lead.id; lines.append(f"For: {lead.name}")
    elif _s(a.get("lead_name")):
        # "remind me to call Test Client..." - attach the lead by name (owner's test, 11/01: it was
        # saved with no lead). Two people with that name: ask which.
        name = _s(a["lead_name"])
        rows = (await db.execute(select(Lead).where(Lead.owner_id == user.id, Lead.hidden_at.is_(None), Lead.name.ilike(f"%{name}%")).limit(3))).scalars().all()
        if len(rows) > 1:
            raise ActionError("Which one? " + ", ".join(f"{l.name} ({l.email or l.phone or 'no email'})" for l in rows) + " - give me the lead_id.")
        if rows:
            body["lead_id"] = rows[0].id; lines.append(f"For: {rows[0].name}")
    due = _when(a)
    if due:
        body["due_at"] = due; body["all_day"] = not _s(a.get("due_time"))
        lines.append(f"Due: {_nice_date(due)}" + ("" if body["all_day"] else f" at {_nice_time(due[11:16])}"))
    if _s(a.get("notes")) and _s(a["notes"]).lower() != title.lower():   # not the title again
        body["notes"] = _s(a["notes"]); lines.append(f"Notes: {body['notes'][:200]}")
    if _s(a.get("suggested_message")):
        body["suggested_message"] = _s(a["suggested_message"]); lines.append(f"Message to send: “{body['suggested_message'][:300]}”")
    return Prepared("Add a follow-up", lines, {"body": body})


async def _r_new_task(db, user, p):
    out = await _run_route(user, "POST", "/tasks", p["body"])
    return {"message": "Follow-up added (and on your calendar if it's connected).", "link": f"/leads/tasks?task={(out or {}).get('id', '')}"}

_simple("create_followup", "Add a follow-up (to-do) for the agent, optionally for one lead, with a due date/time (Eastern) and a message to send. Dates YYYY-MM-DD, times HH:MM 24h. When it's about a person, pass lead_id, or lead_name and it finds them. notes only when there's more than the title says.",
        _obj({"title": {"type": "string"}, "lead_id": {"type": "string"}, "lead_name": {"type": "string"}, "kind": {"type": "string", "enum": ["call", "text", "email", "showing", "general"]},
              "due_date": {"type": "string"}, "due_time": {"type": "string"}, "notes": {"type": "string"}, "suggested_message": {"type": "string"}}, ["title"]),
        _p_new_task, _r_new_task)


async def _p_edit_task(db, user, a):
    t = await _task(db, user, a)
    body, lines = {}, []
    if isinstance(a.get("done"), bool):
        body["completed"] = a["done"]; lines.append("Check it off" + (" (also marks them contacted)" if t.lead_id else "") if a["done"] else "Reopen it")
    due = _when(a)
    if due:
        body["due_at"] = due; body["all_day"] = not _s(a.get("due_time"))
        lines.append(f"Move to {_nice_date(due)}" + ("" if body["all_day"] else f" at {_nice_time(due[11:16])}"))
    if _s(a.get("title")):
        body["title"] = _s(a["title"])[:200]; lines.append(f"New title: {body['title']}")
    # A note is added below what's there, as it is when they tap - never a replacement (11/03: it
    # wiped the follow-up's earlier notes).
    add_note = _s(a.get("notes"))
    if add_note:
        lines.append(f"Add to its notes: {add_note[:200]}" if _s(t.notes) else f"Notes: {add_note[:200]}")
    if not body and not add_note:
        raise ActionError("What should change on that follow-up?")
    return Prepared(f"Change “{t.title}”", lines, {"task_id": t.id, "body": body, **({"add_note": add_note} if add_note else {})})


async def _r_edit_task(db, user, p):
    body = dict(p["body"])
    if p.get("add_note"):
        t = await _task(db, user, p)
        body["notes"] = f"{t.notes.rstrip()}\n{p['add_note']}" if _s(t.notes) else p["add_note"]
    await _run_route(user, "PATCH", f"/tasks/{p['task_id']}", body)
    return {"message": "Saved.", "link": f"/leads/tasks?task={p['task_id']}"}

_simple("update_followup", "Check off, reopen, reschedule or rename one follow-up, or add to its notes (`notes` is added below what is there - never send the old notes back). Use task ids from list_followups.",
        _obj({"task_id": {"type": "string"}, "done": {"type": "boolean"}, "due_date": {"type": "string"}, "due_time": {"type": "string"},
              "title": {"type": "string"}, "notes": {"type": "string"}}, ["task_id"]), _p_edit_task, _r_edit_task)

RULES = {"open_house_signin": "Open house sign-in", "similar_homes_qr": "Similar Homes QR scan", "second_visit_request": "Second visit request",
         "disclosure_request": "Disclosure request", "showcase_setup": "Showcase set up", "buyer_form": "Buyer form", "how_can_we_help": "How can we help form",
         "form_submission": "Form submission", "invite_rsvp": "Invite RSVP", "showing_request": "Showing request", "showcase_comment": "Showcase comment",
         "showcase_likes": "Showcase likes", "showcase_search_request": "Search change request", "listing_inquiry": "Listing inquiry",
         "website_signin": "Website sign-in", "private_feedback": "Private feedback"}
WHEN = {"same_day": "Soon", "later_today": "In 2 hrs", "next_day": "Next day"}


async def _p_rules(db, user, a):
    body, lines = {}, []
    if isinstance(a.get("all_automatic_followups"), bool):
        body["enabled"] = a["all_automatic_followups"]; lines.append(f"Automatic follow-ups: {_yn(a['all_automatic_followups'])}")
    rule = _s(a.get("rule"))
    if rule:
        if rule not in RULES:
            raise ActionError("I don't know that follow-up rule.")
        r = {}
        if isinstance(a.get("rule_on"), bool):
            r["enabled"] = a["rule_on"]; lines.append(f"{RULES[rule]} follow-ups: {_yn(a['rule_on'])}")
        if a.get("when") in WHEN:
            r["when"] = a["when"]; lines.append(f"{RULES[rule]} follow-ups land: {WHEN[a['when']]}")
        if r:
            body["rules"] = {rule: r}
    if not body:
        raise ActionError("Which follow-up setting should change?")
    return Prepared("Automatic follow-up settings", lines, {"body": body})


async def _r_rules(db, user, p):
    await _run_route(user, "PUT", "/tasks/auto-rules", p["body"])
    return {"message": "Saved.", "link": "/leads/tasks"}

_simple("set_followup_rules", "Turn all automatic follow-ups on/off, or one rule on/off or change when it lands (same_day='Soon', later_today='In 2 hrs', next_day).",
        _obj({"all_automatic_followups": {"type": "boolean"}, "rule": {"type": "string", "enum": list(RULES)}, "rule_on": {"type": "boolean"},
              "when": {"type": "string", "enum": list(WHEN)}}), _p_rules, _r_rules)


# ================================================================ SHOWCASES

_SEARCH_PROPS = {
    "min_price": {"type": "integer"}, "max_price": {"type": "integer"}, "min_beds": {"type": "integer"}, "max_beds": {"type": "integer"},
    "min_baths": {"type": "number"}, "max_baths": {"type": "number"}, "min_sqft": {"type": "number"}, "max_sqft": {"type": "number"},
    "min_year_built": {"type": "integer", "description": "Built in or after this year. 'Newer builds / newer construction' with no year given = about the last 15 years (e.g. 2011)."},
    "cities": {"type": "array", "items": {"type": "string"}, "description": "Towns as people say them (Bryn Mawr, Gladwyne, Wayne) - almost always this one."},
    "townships": {"type": "array", "items": {"type": "string"}, "description": "Only a township by its township name (Lower Merion, Radnor) - never a town like Bryn Mawr."},
    "school_districts": {"type": "array", "items": {"type": "string"}},
    "is_single_family": {"type": "boolean"}, "is_town_house": {"type": "boolean"}, "is_condo": {"type": "boolean"}, "is_multi_family": {"type": "boolean"},
}


def _search_lines(a: dict) -> tuple[dict, list]:
    body = {k: a[k] for k in _SEARCH_PROPS if a.get(k) not in (None, "", [])}
    lines = []
    # One side only = just that side ("any" would read as clearing the other side on a search change)
    if "min_price" in body and "max_price" in body:
        lines.append(f"Price: ${body['min_price']:,} - ${body['max_price']:,}")
    elif "max_price" in body:
        lines.append(f"Top price: ${body['max_price']:,}")
    elif "min_price" in body:
        lines.append(f"Lowest price: ${body['min_price']:,}")
    if "min_beds" in body: lines.append(f"{body['min_beds']}+ beds")
    if "min_baths" in body: lines.append(f"{body['min_baths']}+ baths")
    if "min_year_built" in body: lines.append(f"Built {body['min_year_built']} or newer")
    for k, label in (("cities", "Towns"), ("townships", "Townships"), ("school_districts", "School districts")):
        if k in body: lines.append(f"{label}: {', '.join(body[k])}")
    types = [t for k, t in (("is_single_family", "single family"), ("is_town_house", "townhouse"), ("is_condo", "condo"), ("is_multi_family", "multi-family")) if body.get(k)]
    if types: lines.append("Types: " + ", ".join(types))
    return body, lines


async def _p_new_showcase(db, user, a):
    name, email = _s(a.get("client_name")), _s(a.get("client_email")).lower()
    if not name or "@" not in email:
        raise ActionError("I need the client's name and email.")
    search, lines = _search_lines(a)
    if not any(k in search for k in ("cities", "townships", "school_districts")):
        raise ActionError("Which towns, townships or school districts should their search cover?")
    await _towns_not_townships(db, search, lines)
    send = bool(a.get("email_client"))
    # "Don't email her yet" = no emails to the client at all - the welcome AND the daily new-match
    # emails (owner's test, 11/01: only the welcome was skipped). Default: their daily emails run.
    quiet = a.get("client_emails") is False
    body = {"name": f"{name}'s Showcase", "visitor_name": name, "visitor_email": email, "visitor_phone": _s(a.get("client_phone")),
            "visiting_reason": "buying", "has_agent": "no", "email_client": send and not quiet, **search}
    lines = [f"Client: {name} ({email})"] + lines + ["Also adds them to your leads"]
    if quiet:
        lines.append("No emails to them for now - turn their emails on in the Showcase whenever you're ready")
    elif not send:
        lines.append("No welcome email now; they get new matches by email each day")
    return Prepared(f"Make a Showcase for {name}", lines, {"body": body, "quiet": quiet},
                    warning=f"This emails {name} their Showcase right away." if send and not quiet else None)


async def _share(db, user, path: str | None, url: str | None = None) -> dict | None:
    """The link a client would get by text (owner, 10/31): `url` is what they'd receive (their own
    domain when they use one), `preview` the openhousepal.com path its preview card is read from."""
    if not path:
        return None
    from app.services.website.site_service import resolve_owner_public_base_url
    try:
        base = (await resolve_owner_public_base_url(db, user)).rstrip("/")
    except Exception:  # noqa: BLE001
        base = ""
    return {"url": url or f"{base}{path}", "preview": path}


async def _r_new_showcase(db, user, p):
    out = await _run_route(user, "POST", "/collections/create-manually", p["body"])
    cid = (out or {}).get("id")
    if cid and p.get("quiet"):
        await _run_route(user, "PATCH", f"/collections/{cid}/notifications", {"notify_visitor": False, "notify_agent": True})
    share = None
    if cid:
        from app.services.showcase_links import showcase_path
        col = await db.get(Collection, cid)
        share = await _share(db, user, (await showcase_path(col) or (f"/showcase/{col.share_token}" if col and col.share_token else None)) if col else None)
    return {"message": "Showcase made.", "link": f"/showcases?showcase={cid}" if cid else "/showcases", "share": share}

_simple("create_showcase", "Make a new Showcase (saved home search that updates itself) for a client - also adds them as a lead. Needs at least one town/township/school district. email_client=true emails them their Showcase now. client_emails=false when they say not to email the client (no emails at all until the agent turns them on; otherwise they get daily new-match emails).",
        _obj({"client_name": {"type": "string"}, "client_email": {"type": "string"}, "client_phone": {"type": "string"}, "email_client": {"type": "boolean"}, "client_emails": {"type": "boolean"}, **_SEARCH_PROPS},
             ["client_name", "client_email"]), _p_new_showcase, _r_new_showcase, SHOWCASE)


async def _towns_not_townships(db, search: dict, lines: list) -> None:
    """A place put under townships that no listing has as its township ("Bryn Mawr" - a town, not a
    township, so it matched 0 homes) moves to towns when listings do have it as their town."""
    moved = []
    for t in list(search.get("townships") or []):
        is_twp = (await db.execute(select(Property.id).where(func.lower(Property.township) == t.strip().lower()).limit(1))).first()
        is_town = not is_twp and (await db.execute(select(Property.id).where(func.lower(Property.city) == t.strip().lower()).limit(1))).first()
        if is_town:
            search["townships"].remove(t); search.setdefault("cities", []).append(t); moved.append(t)
    if moved:
        if not search.get("townships"):
            search.pop("townships", None)
        lines[:] = [l for l in lines if not l.startswith(("Towns:", "Townships:"))]
        for k, label in (("cities", "Towns"), ("townships", "Townships")):
            if search.get(k):
                lines.append(f"{label}: {', '.join(search[k])}")


async def _p_showcase_search(db, user, a):
    c = await _showcase(db, user, a)
    search, lines = _search_lines(a)
    if not search:
        raise ActionError("What should change in their search?")
    await _towns_not_townships(db, search, lines)
    # Towns replace a distance / drawn-area search - left in place, the old circle adds its homes on
    # top (owner, 10/31: Bryn Mawr + Gladwyne showed Penn Valley, Narberth, Bala Cynwyd, Ardmore).
    if any(k in search for k in ("cities", "townships")):
        pref = (await db.execute(select(CollectionPreferences).where(CollectionPreferences.collection_id == c.id))).scalar_one_or_none()
        if pref and ((pref.diameter and pref.lat is not None) or pref.search_polygon):
            search.update({"address": None, "lat": None, "long": None, "diameter": None, "search_polygon": None})
            lines.append("Drops the old distance / drawn-area search, so only these towns count")
    return Prepared(f"Change {_sc_name(c)} search", lines + ["Only these parts change; the rest stays"], {"collection_id": c.id, "body": search},
                    warning=f"{c.visitor_name or 'Your client'} hears about the new search in their next daily email.")


async def _r_showcase_search(db, user, p):
    out = await _run_route(user, "PUT", f"/collections/{p['collection_id']}/update-preferences-and-refresh", p["body"])
    n = (out or {}).get("properties_count")
    return {"message": f"Search updated{f' - {n} homes match now' if n is not None else ''}.", "link": f"/showcases?showcase={p['collection_id']}"}

_simple("update_showcase_search", "Change a Showcase's search (price, beds, baths, size, year built, towns, types). Only the fields given change. Use collection_id from list_showcases.",
        _obj({"collection_id": {"type": "string"}, **_SEARCH_PROPS}, ["collection_id"]), _p_showcase_search, _r_showcase_search, SHOWCASE)


async def _p_showcase_state(db, user, a):
    c = await _showcase(db, user, a)
    body, lines, warn = {}, [], None
    if isinstance(a.get("awake"), bool):
        body["status"] = "ACTIVE" if a["awake"] else "INACTIVE"
        lines.append("Wake it up - new homes and their emails start again" if a["awake"] else "Put it to sleep - no new homes or emails until you wake it")
    if isinstance(a.get("starred"), bool):
        body["favorite"] = a["starred"]; lines.append("Star it" if a["starred"] else "Remove the star")
    if isinstance(a.get("email_client"), bool) or isinstance(a.get("email_me"), bool):
        body["notify_visitor"] = a["email_client"] if isinstance(a.get("email_client"), bool) else c.notify_visitor
        body["notify_agent"] = a["email_me"] if isinstance(a.get("email_me"), bool) else c.notify_agent
        lines.append(f"Emails to {c.visitor_name or 'client'}: {_yn(body['notify_visitor'])}; emails to you: {_yn(body['notify_agent'])}")
    if not body:
        raise ActionError("What should change on that Showcase?")
    return Prepared(_sc_name(c), lines, {"collection_id": c.id, "body": body})


async def _r_showcase_state(db, user, p):
    cid, b = p["collection_id"], p["body"]
    if "status" in b:
        await _run_route(user, "PATCH", f"/collections/{cid}/status", {"status": b["status"]})
    if "favorite" in b:
        await _run_route(user, "PUT", f"/collections/{cid}/favorite", {"favorite": b["favorite"]})
    if "notify_visitor" in b:
        await _run_route(user, "PATCH", f"/collections/{cid}/notifications", {"notify_visitor": b["notify_visitor"], "notify_agent": b["notify_agent"]})
    return {"message": "Saved.", "link": f"/showcases?showcase={cid}"}

_simple("set_showcase", "Wake up / put to sleep, star, or turn emails on/off (to the client, to the agent) for one Showcase.",
        _obj({"collection_id": {"type": "string"}, "awake": {"type": "boolean"}, "starred": {"type": "boolean"}, "email_client": {"type": "boolean"}, "email_me": {"type": "boolean"}}, ["collection_id"]),
        _p_showcase_state, _r_showcase_state, SHOWCASE)


async def _p_add_home(db, user, a):
    c = await _showcase(db, user, a)
    key = _need_id(a, "listing_key")
    prop = await _one(db, select(Property).where(Property.listing_key == key), "That home isn't in the listings yet.")
    return Prepared(f"Add a home to {_sc_name(c)}", [tidy_caps(", ".join(x for x in (prop.street_address, prop.city) if x)) or prop.full_address or key,"Marked Recommended, shows as new for them"],
                    {"collection_id": c.id, "listing_key": key}, warning=f"{c.visitor_name or 'Your client'} sees it in their next Showcase email.")


async def _r_add_home(db, user, p):
    await _run_route(user, "POST", f"/collections/{p['collection_id']}/add-home", {"listing_key": p["listing_key"]})
    return {"message": "Added.", "link": f"/showcases?showcase={p['collection_id']}"}

_simple("add_home_to_showcase", "Add a specific home (listing_key from search_properties) to a client's Showcase as Recommended.",
        _obj({"collection_id": {"type": "string"}, "listing_key": {"type": "string"}}, ["collection_id", "listing_key"]), _p_add_home, _r_add_home, SHOWCASE)


async def _p_tour(db, user, a):
    tid = _need_id(a, "tour_id")
    tour = await _one(db, select(PropertyTour).join(Collection, Collection.id == PropertyTour.collection_id)
                      .where(PropertyTour.id == tid, Collection.owner_id == user.id), "I couldn't find that tour request.")
    if not _s(a.get("time")):
        raise ActionError("What time is the tour?")
    d, t = _date_ok(a.get("date")), _time_ok(a.get("time"))   # HH:MM - what the Showcase page saves
    return Prepared(f"Confirm {tour.visitor_name + chr(39) + 's' if tour.visitor_name else 'the'} tour",[f"{_nice_date(d)} at {_nice_time(t)}","Only confirm once it's booked and the listing agent accepted"],
                    {"tour_id": tid, "date": d, "time": t}, warning=f"This emails {tour.visitor_name or 'your client'} that the tour is confirmed.", confirm_label="Confirm tour")


async def _r_tour(db, user, p):
    await _run_route(user, "PATCH", f"/collections/tours/{p['tour_id']}/confirm", {"confirmed_date": p["date"], "confirmed_time": p["time"]})
    return {"message": "Tour confirmed.", "link": "/showcases"}

_simple("confirm_showcase_tour", "Confirm a client's tour request with the booked date (YYYY-MM-DD) and time. Tour ids from get_showcase_tours.",
        _obj({"tour_id": {"type": "string"}, "date": {"type": "string"}, "time": {"type": "string"}}, ["tour_id", "date", "time"]), _p_tour, _r_tour, SHOWCASE)


async def _p_comment(db, user, a):
    c = await _showcase(db, user, a)
    pid, text = _need_id(a, "property_id"), _s(a.get("text"))
    if not text:
        raise ActionError("What should the reply say?")
    return Prepared(f"Reply in {_sc_name(c)}", [f"“{text[:600]}”"], {"collection_id": c.id, "property_id": pid, "text": text[:4000]},
                    warning=f"This emails {c.visitor_name or 'your client'} your reply.", confirm_label="Send reply")


async def _r_comment(db, user, p):
    await _run_route(user, "POST", f"/collections/{p['collection_id']}/properties/{p['property_id']}/comments", {"content": p["text"]})
    return {"message": "Reply sent.", "link": f"/showcases?showcase={p['collection_id']}"}

_simple("reply_to_showcase_comment", "Reply to a client's comment on a home in their Showcase.",
        _obj({"collection_id": {"type": "string"}, "property_id": {"type": "string"}, "text": {"type": "string"}}, ["collection_id", "property_id", "text"]), _p_comment, _r_comment, SHOWCASE)


# ================================================================ OPEN HOUSES

async def _p_oh_switches(db, user, a):
    oh = await _oh(db, user, a)
    info = await _run_route(user, "GET", f"/api/open-houses/{oh.id}")
    body, lines = {}, []
    if isinstance(a.get("guest_showcases"), bool):
        if a["guest_showcases"] and not (info or {}).get("showcases_available", True):
            raise ActionError("Guest Showcases come with Premium and IDX.")
        body["auto_showcase"] = a["guest_showcases"]; lines.append(f"Guest Showcases: {_yn(a['guest_showcases'])}" + (" (for guests who sign in from now on)" if a["guest_showcases"] else ""))
    if isinstance(a.get("similar_homes"), bool):
        if a["similar_homes"] and not (oh.similar_properties_snapshot or []):
            raise ActionError("Pick the similar homes first (open the kit's Similar Homes and choose them), then I can turn it on.")
        body["similar_homes"] = a["similar_homes"]; lines.append(f"Similar Homes: {_yn(a['similar_homes'])}")
    if isinstance(a.get("guest_pass"), bool):
        if a["guest_pass"] and not (info or {}).get("guest_pass_available", True):
            raise ActionError("Guest Pass comes with the MLS plans.")
        body["guest_pass"] = a["guest_pass"]; lines.append(f"Guest Pass: {_yn(a['guest_pass'])}")
    if a.get("followups") in ("on", "no_calendar", "off"):
        body["followups_mode"] = a["followups"]
        lines.append({"on": "Guest follow-ups: on (and on your calendar)", "no_calendar": "Guest follow-ups: in OpenHousePal only", "off": "Guest follow-ups: off"}[a["followups"]])
    if not body:
        raise ActionError("Which switch should change?")
    return Prepared(f"Open house at {_oh_name(oh)}", lines, {"open_house_id": oh.id, "body": body})


async def _r_oh_switches(db, user, p):
    await _run_route(user, "PUT", f"/api/open-houses/{p['open_house_id']}/guest-settings", p["body"])
    return {"message": "Switches saved.", "link": "/open-houses"}

_simple("set_open_house_switches", "Turn an open house's switches on/off: Guest Showcases, Similar Homes, Guest Pass, and guest follow-ups (on / no_calendar / off). open_house_id from search_open_houses.",
        _obj({"open_house_id": {"type": "string"}, "guest_showcases": {"type": "boolean"}, "similar_homes": {"type": "boolean"}, "guest_pass": {"type": "boolean"},
              "followups": {"type": "string", "enum": ["on", "no_calendar", "off"]}}, ["open_house_id"]), _p_oh_switches, _r_oh_switches)


async def _p_oh_look(db, user, a):
    oh = await _oh(db, user, a)
    dark = bool(a.get("midnight"))
    return Prepared(f"Sign-in page look for {_oh_name(oh)}", [f"{'Midnight (dark)' if dark else 'Ivory (light)'} sign-in page", "The printed flyer doesn't change"], {"open_house_id": oh.id, "dark": dark})


async def _r_oh_look(db, user, p):
    await _run_route(user, "PUT", f"/api/open-houses/{p['open_house_id']}/qr-dark-mode", {"qr_dark_mode": p["dark"]})
    return {"message": "Saved.", "link": "/open-houses"}

_simple("set_open_house_sign_in_look", "Set an open house's on-screen sign-in page to Midnight (dark) or Ivory (light).",
        _obj({"open_house_id": {"type": "string"}, "midnight": {"type": "boolean"}}, ["open_house_id", "midnight"]), _p_oh_look, _r_oh_look)


async def _p_oh_note(db, user, a):
    oh = await _oh(db, user, a)
    text = _s(a.get("text"))
    if not text:
        raise ActionError("What should the note say?")
    # Just the new line - it's added to the note as it is when they tap (11/03: the whole note saved
    # here wiped anything typed on the Open Houses page while the card waited).
    return Prepared(f"Add to your note on {_oh_name(oh)}", [f"“{text[:400]}”", "Added below what's already there"], {"open_house_id": oh.id, "text": text})


async def _r_oh_note(db, user, p):
    if "text" in p:
        oh = await _oh(db, user, p)
        notes = f"{oh.notes.rstrip()}\n{p['text']}" if _s(oh.notes) else p["text"]
    else:
        notes = p["notes"]  # a card made before 11/03
    await _run_route(user, "PUT", f"/api/open-houses/{p['open_house_id']}/note", {"notes": notes})
    return {"message": "Note saved.", "link": "/open-houses"}

_simple("add_open_house_note", "Add a line to the agent's private note on an open house (lockbox, turnout, feedback). Keeps what's there.",
        _obj({"open_house_id": {"type": "string"}, "text": {"type": "string"}}, ["open_house_id", "text"]), _p_oh_note, _r_oh_note)


async def _p_guest_showcase(db, user, a):
    vid = _need_id(a, "visitor_id")
    v = await _one(db, select(OpenHouseVisitor).join(OpenHouseEvent, OpenHouseEvent.id == OpenHouseVisitor.open_house_event_id)
                   .where(OpenHouseVisitor.id == vid, OpenHouseEvent.agent_id == user.id), "I couldn't find that guest.")
    on = bool(a.get("on"))
    return Prepared(f"{'Give' if on else 'Stop'} {v.full_name} {'a' if on else 'their'} Showcase", ["Just for this guest - beats the open house's setting"],
                    {"visitor_id": vid, "on": on}, warning=(f"This emails {v.full_name} their Showcase invite (at your usual invite time)." if on else None))


async def _r_guest_showcase(db, user, p):
    await _run_route(user, "PUT", f"/api/visitors/{p['visitor_id']}/showcase", {"on": p["on"]})
    return {"message": "Saved.", "link": "/open-houses"}

_simple("set_guest_showcase", "Turn one open house guest's Showcase on or off (visitor_id from get_open_house_guests). Premium/IDX.",
        _obj({"visitor_id": {"type": "string"}, "on": {"type": "boolean"}}, ["visitor_id", "on"]), _p_guest_showcase, _r_guest_showcase, SHOWCASE)


async def _p_refresh_similar(db, user, a):
    oh = await _oh(db, user, a)
    return Prepared(f"Refresh Similar Homes for {_oh_name(oh)}", ["Updates prices and drops homes that are no longer for sale"], {"open_house_id": oh.id})


async def _r_refresh_similar(db, user, p):
    await _run_route(user, "POST", f"/api/open-houses/{p['open_house_id']}/snapshot/refresh")
    return {"message": "Similar Homes refreshed.", "link": "/open-houses"}

_simple("refresh_similar_homes", "Refresh an open house's Similar Homes sheet (new prices, drops sold homes).", _obj({"open_house_id": {"type": "string"}}, ["open_house_id"]),
        _p_refresh_similar, _r_refresh_similar, MLS)


async def _p_guest_req(db, user, a):
    rid = _need_id(a, "request_id")
    r = await _one(db, select(OpenHouseFormRequest).join(OpenHouseEvent, OpenHouseEvent.id == OpenHouseFormRequest.open_house_event_id)
                   .where(OpenHouseFormRequest.id == rid, OpenHouseEvent.agent_id == user.id), "I couldn't find that request.")
    what = "seller disclosure as sent" if r.wants_disclosure else "second visit as scheduled"
    return Prepared(f"Mark {r.full_name}'s {what}", ["Clears it and checks off its follow-up", "Doesn't email them - send the disclosure yourself"], {"request_id": rid})


async def _r_guest_req(db, user, p):
    await _run_route(user, "PATCH", f"/api/open-houses/form-requests/{p['request_id']}/complete")
    return {"message": "Marked done.", "link": "/open-houses"}

_simple("mark_guest_request_done", "Mark an open house guest's disclosure request as Sent or second-visit request as Scheduled (request_id from get_guest_requests).",
        _obj({"request_id": {"type": "string"}}, ["request_id"]), _p_guest_req, _r_guest_req)


async def _host_req(db, user, a) -> OpenHouseHostRequest:
    return await _one(db, select(OpenHouseHostRequest).where(OpenHouseHostRequest.id == _need_id(a, "request_id"),
                                                           OpenHouseHostRequest.listing_owner_agent_id == user.id), "I couldn't find that host request.")


async def _agent_name(db, uid) -> str:
    u = await db.get(User, uid) if uid else None
    return f"{u.first_name} {u.last_name}".strip() if u else "the agent"


async def _p_answer_host(db, user, a):
    r = await _host_req(db, user, a)
    if r.status not in ("pending", "countered"):
        raise ActionError("That request isn't waiting on you any more - it was already answered or withdrawn.")
    if r.requested_date and str(r.requested_date)[:10] < _date.today().isoformat():
        raise ActionError("That day has already passed, so there's nothing to answer.")
    who = await _agent_name(db, r.requesting_agent_id)
    when = f"{_nice_date(str(r.requested_date))}, {_nice_time(str(r.requested_start_time))}-{_nice_time(str(r.requested_end_time))}"
    answer = _s(a.get("answer"))
    if answer == "approve":
        return Prepared(f"Approve {who} to host", [when, "Other requests for that day get declined"], {"request_id": r.id, "answer": answer},
                        warning=f"This emails {who} (and any agent auto-declined).", confirm_label="Approve")
    if answer == "decline":
        return Prepared(f"Decline {who}'s request to host", [when], {"request_id": r.id, "answer": answer}, warning=f"This emails {who}.", confirm_label="Decline it")
    if answer == "suggest_time":
        d, s, e = _date_ok(a.get("date")), _time_ok(a.get("start_time")), _time_ok(a.get("end_time"))
        body = {"counter_date": d, "counter_start_time": f"{s}:00", "counter_end_time": f"{e}:00", "counter_message": _s(a.get("message")) or None}
        return Prepared(f"Suggest another time to {who}", [f"{_nice_date(d)}, {_nice_time(s)}-{_nice_time(e)}", "You can only suggest another time once"],
                        {"request_id": r.id, "answer": answer, "body": body}, warning=f"This emails {who}.", confirm_label="Send it")
    raise ActionError("Approve, decline or suggest another time?")


async def _r_answer_host(db, user, p):
    path = {"approve": "approve", "decline": "decline", "suggest_time": "counter"}[p["answer"]]
    await _run_route(user, "POST", f"/api/open-house-manager/host-requests/{p['request_id']}/{path}", p.get("body"))
    return {"message": {"approve": "Approved.", "decline": "Declined.", "suggest_time": "Sent."}[p["answer"]], "link": "/open-houses/my-listings"}

_simple("answer_host_request", "Answer another agent's request to host one of the agent's listings: approve, decline, or suggest_time (date YYYY-MM-DD, start/end HH:MM). request_id from get_host_requests.",
        _obj({"request_id": {"type": "string"}, "answer": {"type": "string", "enum": ["approve", "decline", "suggest_time"]}, "date": {"type": "string"},
              "start_time": {"type": "string"}, "end_time": {"type": "string"}, "message": {"type": "string"}}, ["request_id", "answer"]),
        _p_answer_host, _r_answer_host, MLS)


async def _p_ask_host(db, user, a):
    key = _need_id(a, "listing_key")
    prop = await _one(db, select(Property).where(Property.listing_key == key), "I couldn't find that listing.")
    if not user.mls_id or user.mls_id not in (prop.list_agent_mls_id, prop.co_list_agent_mls_id):
        raise ActionError("That isn't one of your listings.")
    d = _date_ok(a.get("date"))
    body = {"listing_key": key, "requested_date": d, "message": _s(a.get("message")) or None}
    if _s(a.get("start_time")):
        s, e = _time_ok(a.get("start_time")), _time_ok(a.get("end_time"))
        body.update(requested_start_time=f"{s}:00", requested_end_time=f"{e}:00"); when = f"{_nice_date(d)}, {_nice_time(s)}-{_nice_time(e)}"
    else:
        body["is_anytime"] = True; when = f"{_nice_date(d)}, any time"
    return Prepared(f"Ask your team to host {tidy_caps(prop.street_address)}",[when] + ([f"Message: “{body['message']}”"] if body["message"] else []), {"body": body},
                    warning="This emails everyone on your team.", confirm_label="Send ask")


async def _r_ask_host(db, user, p):
    await _run_route(user, "POST", "/api/open-house-manager/team/broadcasts", p["body"])
    return {"message": "Asked your team.", "link": "/open-houses/my-listings"}

_simple("ask_team_to_host", "Ask the agent's team to host an open house at one of their own listings (listing_key), on a date with optional start/end times.",
        _obj({"listing_key": {"type": "string"}, "date": {"type": "string"}, "start_time": {"type": "string"}, "end_time": {"type": "string"}, "message": {"type": "string"}},
             ["listing_key", "date"]), _p_ask_host, _r_ask_host, MLS)


async def _p_request_host(db, user, a):
    key = _need_id(a, "listing_key")
    prop = await _one(db, select(Property).where(Property.listing_key == key), "I couldn't find that listing.")
    d, s, e = _date_ok(a.get("date")), _time_ok(a.get("start_time")), _time_ok(a.get("end_time"))
    msg = _s(a.get("message")) or None
    # The same who-may-ask rule the request route enforces - checked here so the card never offers
    # "Send request" for an agent who'd refuse it (11/02: the card failed only after the tap)
    from app.services.open_house_manager_service import _is_eligible_requester
    owner_mls = prop.list_agent_mls_id or prop.co_list_agent_mls_id
    owner = None
    if owner_mls and owner_mls.startswith("user:"):
        owner = await db.get(User, owner_mls[5:])
    elif owner_mls:
        owner = (await db.execute(select(User).where(User.mls_id == owner_mls).order_by(User.test_copy_of_user_id.isnot(None), User.created_at))).scalars().first()
    if owner and owner.id != user.id and not await _is_eligible_requester(db, owner, user):
        raise ActionError(f"{prop.list_agent_full_name or 'That listing agent'} only takes host requests from agents at their brokerage or on their team, "
                          "so I can't send this one.")
    return Prepared(f"Ask to host {tidy_caps(prop.street_address)}", [f"{_nice_date(d)}, {_nice_time(s)}-{_nice_time(e)}", f"Listing agent: {prop.list_agent_full_name or 'the listing agent'}"]
                    + ([f"Message: “{msg}”"] if msg else []),
                    {"body": {"listing_key": key, "requested_date": d, "requested_start_time": f"{s}:00", "requested_end_time": f"{e}:00", "message": msg}},
                    warning=f"This emails {prop.list_agent_full_name or 'the listing agent'}.", confirm_label="Send request")


async def _r_request_host(db, user, p):
    out = await _run_route(user, "POST", "/api/open-house-manager/requests", p["body"])
    if isinstance(out, dict) and out.get("on_platform") is False:
        return {"message": "That listing agent isn't on OpenHousePal yet, so nothing was sent - Discovery has an example email you can send them yourself.", "link": "/discovery"}
    return {"message": "Request sent.", "link": "/discovery"}

_simple("request_to_host", "Ask a listing agent to let this agent host an open house at their listing (listing_key from search_properties), with date and start/end times.",
        _obj({"listing_key": {"type": "string"}, "date": {"type": "string"}, "start_time": {"type": "string"}, "end_time": {"type": "string"}, "message": {"type": "string"}},
             ["listing_key", "date", "start_time", "end_time"]), _p_request_host, _r_request_host, MLS)


async def _p_counter_reply(db, user, a):
    r = await _one(db, select(OpenHouseHostRequest).where(OpenHouseHostRequest.id == _need_id(a, "request_id"), OpenHouseHostRequest.requesting_agent_id == user.id),
                   "I couldn't find that request.")
    # Only a request where the listing agent actually suggested another time (11/02 review: a card said
    # "their suggested time" with no date for an already-approved request)
    if r.status != "countered" or not r.counter_date:
        raise ActionError("The listing agent hasn't suggested another time on that request, so there's nothing to accept or decline.")
    if str(r.counter_date)[:10] < _date.today().isoformat():
        raise ActionError("The time they suggested has already passed.")
    who = await _agent_name(db, r.listing_owner_agent_id)
    yes = bool(a.get("accept"))
    when = f"{_nice_date(str(r.counter_date))}, {_nice_time(str(r.counter_start_time))}-{_nice_time(str(r.counter_end_time))}"
    return Prepared(f"{'Accept' if yes else 'Decline'} {who}'s suggested time", [when], {"request_id": r.id, "accept": yes}, warning=f"This emails {who}.")


async def _r_counter_reply(db, user, p):
    await _run_route(user, "POST", f"/api/open-house-manager/host-requests/{p['request_id']}/counter/{'accept' if p['accept'] else 'decline'}")
    return {"message": "Sent.", "link": "/discovery"}

_simple("answer_suggested_host_time", "Accept or decline the other time a listing agent suggested for the agent's hosting request (request_id from get_my_hosting_requests).",
        _obj({"request_id": {"type": "string"}, "accept": {"type": "boolean"}}, ["request_id", "accept"]), _p_counter_reply, _r_counter_reply, MLS)


async def _p_activity_read(db, user, a):
    return Prepared("Mark all activity as read", ["Clears the unread dots on Activity and the bell", "Can't be undone"], {})


async def _r_activity_read(db, user, p):
    await _run_route(user, "POST", "/api/v1/notifications/mark-all-as-read")
    return {"message": "All caught up.", "link": "/activity"}

_simple("mark_all_activity_read", "Mark all of the agent's activity/notifications as read.", _obj({}), _p_activity_read, _r_activity_read)


# ================================================================ CMA

CMA_SECTIONS = ("subject", "comparison_table", "location", "comparables", "market_context", "charts", "competition", "pricing")
CMA_TYPES = ("SINGLE_FAMILY", "TWIN", "TOWNHOUSE", "CONDO", "MULTI_FAMILY")


async def _p_cma(db, user, a):
    key = _s(a.get("listing_key"))
    typed = _s(a.get("address"))
    if not key and not typed:
        raise ActionError("Which home is the CMA for? Tell me the address.")
    prop = (await _one(db, select(Property).where(Property.listing_key == key), "I couldn't find that home.")) if key else None
    if prop is None and re.match(r"^\d+\s+\S", typed):
        # An address they typed may still be in the MLS records (sold homes too): use that record if it is.
        parts = [x.strip() for x in typed.split(",")]
        q = select(Property).where(Property.street_address.ilike(f"{parts[0]}%"))
        if len(parts) > 1 and not re.match(r"^[A-Za-z]{2}(\s+\d{5})?$", parts[1]):
            q = q.where(func.lower(Property.city) == parts[1].lower())
        found = (await db.execute(q.order_by(Property.mls_list_date.desc().nulls_last()).limit(2))).scalars().all()
        if len(found) == 1 or (found and len(parts) > 1):
            prop, key = found[0], found[0].listing_key
    mode = "buyer" if a.get("for_buyer") else "listing"
    # AI or manual is asked ON the card (owner, 11/01) - two buttons, not a chat question. What one AI
    # run usually costs, as a share of their monthly AI allowance, is shown on the AI choice.
    try:
        est = await _run_route(user, "GET", "/api/cma/ai-estimate")
    except ActionError:
        est = {}
    addr = (tidy_caps(prop.street_address) if prop.street_address else key) if prop else typed
    photos = [x for x in (prop.photos or []) if isinstance(x, str)] if prop and isinstance(prop.photos, list) else []
    facts = {"beds": prop.bedrooms, "baths": prop.bathrooms, "sqft": prop.living_area, "price": prop.price, "city": tidy_caps(prop.city)} if prop else {}
    return Prepared(f"Start a CMA for {addr}", [f"{'Buyer / Offer' if mode == 'buyer' else 'Seller / Listing'} CMA"] +
                    ([f"Client: {_s(a.get('client_name'))}"] if _s(a.get("client_name")) else []) +
                    ([] if prop else ["This home isn't on the MLS - add its address and facts on the card"]),
                    {"listing_key": key or None, "mode": mode, "client_name": _s(a.get("client_name")) or None, "ai": True, "address": addr, "manual": prop is None,
                     "ai_percent": (est or {}).get("percent"), "photo": photos[0] if photos else None, "facts": facts,
                     "condition_photos": [u for u in [_s(a.get("photo_url"))] if u]},
                    confirm_label="Start", picker="cma")


def _adjust_cma(user, payload: dict, picks: dict) -> dict:
    out = {**payload, "ai": bool(picks.get("ai", payload.get("ai")))}
    if picks.get("mode") in ("listing", "buyer"):
        out["mode"] = picks["mode"]
    if "client_name" in picks:
        out["client_name"] = _s(picks.get("client_name"))[:120] or None
    # What the AI can't see in the listing, the report's look, and which sections show (owner, 11/05)
    extra = {k: _s(picks.get(k))[:n] for k, n in (("condition", 120), ("recent_updates", 1000), ("public_remarks", 4000)) if _s(picks.get(k))}
    if picks.get("theme") in ("light", "dark"):
        out["theme"] = picks["theme"]
    if isinstance(picks.get("hidden_sections"), list):
        out["hidden_sections"] = [x for x in picks["hidden_sections"] if x in CMA_SECTIONS]
    out["extra"] = extra
    if payload.get("manual"):
        m = picks.get("subject") if isinstance(picks.get("subject"), dict) else {}
        if not _s(m.get("street_address")) or not isinstance(m.get("latitude"), (int, float)) or not isinstance(m.get("longitude"), (int, float)):
            raise ActionError("Pick the home's address from the list first.")
        num = lambda k: m.get(k) if isinstance(m.get(k), (int, float)) and m.get(k) >= 0 else None
        if out["ai"]:
            need = [label for k, label in (("beds", "bedrooms"), ("full_baths", "full baths"), ("above_grade_finished_area", "finished square feet"), ("year_built", "year built")) if not num(k)]
            if need or m.get("property_type") not in CMA_TYPES:
                raise ActionError("An AI CMA needs " + ", ".join((["the home type"] if m.get("property_type") not in CMA_TYPES else []) + need) + " first.")
        full, half, above, below = num("full_baths") or 0, num("half_baths") or 0, num("above_grade_finished_area") or 0, num("below_grade_finished_area") or 0
        subject = {k: _s(m.get(k)) or None for k in ("street_address", "city", "state", "zipcode", "county")}
        subject.update(source="manual", listing_key=None, latitude=m["latitude"], longitude=m["longitude"], property_type=m.get("property_type") if m.get("property_type") in CMA_TYPES else None,
                       beds=num("beds"), full_baths=full or None, half_baths=half or None, baths=(full + 0.5 * half) or None, above_grade_finished_area=above or None,
                       below_grade_finished_area=below or None, sqft=(above + below) or None, year_built=num("year_built"))
        out["subject"] = subject
        out["address"] = subject["street_address"]
        out["condition_photos"] = [u for u in (picks.get("condition_photos") or []) if isinstance(u, str) and u.startswith("http")][:10]
    return out


_BACKGROUND: set = set()   # strong refs - a bare create_task can be garbage-collected mid-run


async def _ai_cma_in_background(user_id: str, rid: str, subject: dict, address: str) -> None:
    """Picks the comps (the CMA page's own search - it can take a minute or two on a busy server),
    saves them and starts the AI analysis. Runs after the card already answered (owner, 11/01: the
    tap waited 112s, the connection gave up and the card said it failed although the CMA was made).
    Never raises; a dead end posts in the chat."""
    from datetime import date as _date
    from app.database import AsyncSessionLocal
    from app.services.ai import ai_notify
    from app.services.cma_backtest_service import _candidate_to_comparable, wizard_comps
    key, link = f"cma:{rid}", f"/studio/cma/{rid}"
    try:
        async with AsyncSessionLocal() as db:
            user = await db.get(User, user_id)
            try:
                picks, _meta = await wizard_comps(db, subject, _date.today())
            except Exception:
                logger.warning("AI CMA comp search failed", exc_info=True)
                picks = []
            if not picks or user is None:
                await ai_notify.finished(user_id, key, False,
                                         "I made the CMA for {label}, but I couldn't find enough similar homes that sold nearby to pick comps on my own - "
                                         "open it to choose them, then run the AI analysis.", link=link, link_label="Open the CMA")
                return
            await _run_route(user, "PATCH", f"/api/cma/{rid}", {"comparables": [_candidate_to_comparable(c, i) for i, c in enumerate(picks)]})
            await _run_route(user, "POST", f"/api/cma/{rid}/generate-ai-analysis", {"background": True, "include_map_images": True})
            # From here the analysis's own finish (cma_routes) answers the watch.
    except Exception as e:  # noqa: BLE001
        logger.exception("AI CMA background start failed", extra={"cma_id": rid})
        await ai_notify.finished(user_id, key, False, f"The AI CMA for {{label}} couldn't start: {e}. Open it to pick the comps yourself.",
                                 link=link, link_label="Open the CMA")


async def _r_cma(db, user, p):
    if p.get("manual"):
        subject = dict(p.get("subject") or {})
        try:   # the page's own fill-in: school district, township, water and sewer
            area = await call_as_agent(user, "GET", "/api/cma/subject-area", params={"lat": subject["latitude"], "lng": subject["longitude"]}) or {}
            subject.update({k: v for k, v in area.items() if v not in (None, "") and not subject.get(k)})
        except RouteError:
            pass
        if p.get("condition_photos"):
            subject["condition_photos"] = p["condition_photos"]
    else:
        subject = await _run_route(user, "GET", f"/api/cma/subject-from-listing/{p['listing_key']}")
    subject.update(p.get("extra") or {})
    out = await _run_route(user, "POST", "/api/cma", {"mode": p["mode"], "client_name": p["client_name"], "subject": subject})
    rid = (out or {}).get("id")
    link = f"/studio/cma/{rid}" if rid else "/studio/cma"
    if rid and p.get("manual") and not subject.get("photo_url"):
        try:   # the same street picture the page fetches for a home that isn't on the MLS
            await _run_route(user, "POST", f"/api/cma/{rid}/subject-photo/google", {"kind": "street_view"})
        except ActionError:
            pass
    if rid and (p.get("theme") or p.get("hidden_sections")):
        await _run_route(user, "PATCH", f"/api/cma/{rid}", {"presentation": {"default_theme": p.get("theme") or "light", "hidden_sections": p.get("hidden_sections") or []}})
    if not rid or not p.get("ai"):
        return {"message": "CMA started - pick your comps on the CMA page.", "link": link}
    # AI CMA (owner, 11/01): the CMA page's own comp search and picks (cma_backtest_service.wizard_comps
    # - the wizard's rules, run on the server), saved, then the same background AI analysis as
    # "Create AI Analysis" - all after the card answers, so the tap never waits. It keeps going if
    # they close the site; ai_notify posts here when done.
    import asyncio
    from app.services.ai import ai_notify
    from app.services.ai.ai_actions import CARD
    await ai_notify.watch(db, user, f"cma:{rid}", CARD.get() or {}, p.get("address") or "this home")   # before it can finish
    task = asyncio.create_task(_ai_cma_in_background(user.id, rid, (out or {}).get("subject") or subject, p.get("address") or ""))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return {"working": True, "link": link,
            "message": "Making your AI CMA - picking the comps, then the analysis and report, usually 3 to 5 minutes. I'll tell you right here when "
                       "it's done, even if you close the site or open it on another device."}

_simple("start_cma", "Start a CMA for a home, seller/listing or buyer: listing_key from search_properties, or for a home that isn't on the MLS pass address (what they typed) and the card takes its address and facts. "
        "The card asks Seller or Buyer, AI CMA (it picks the comps and runs the analysis - uses AI allowance) or Manual, the client's name, the home's condition and recent updates, and the report's look and sections - don't ask those in chat. "
        "A photo they attached goes in photo_url.",
        _obj({"listing_key": {"type": "string"}, "address": {"type": "string"}, "for_buyer": {"type": "boolean"}, "client_name": {"type": "string"}, "photo_url": {"type": "string"}}), _p_cma, _r_cma, MLS,
        adjust=_adjust_cma)


# ================================================================ SPOTLIGHT / STUDIO

async def _p_spotlight(db, user, a):
    key = _need_id(a, "listing_key")
    prop = await _one(db, select(Property).where(Property.listing_key == key), "I couldn't find that listing.")
    from app.utils.plan_ids import listing_owner_key
    if listing_owner_key(user) not in (prop.list_agent_mls_id, prop.co_list_agent_mls_id):
        raise ActionError("A Spotlight can only be made for one of your own listings.")
    if re.search(r"closed|sold|expired|withdrawn|cancel", (prop.home_status or "").lower()):
        raise ActionError("That listing isn't active any more.")
    have = (await db.execute(select(PropertyWebsite.id).where(PropertyWebsite.owner_id == user.id, PropertyWebsite.listing_key == key))).first()
    style = "editorial_estate" if a.get("style") == "editorial" else "cinematic_luxury"
    # The card shows a small front page to pick the look and color on (owner, 11/01) - the home's
    # cover photo, address and price, in the agent's brand color (a Spotlight wears the Branding color).
    try:
        kit = await _run_route(user, "GET", "/studio/brand-kit") or {}
    except ActionError:
        kit = {}
    photos = prop.photos if isinstance(prop.photos, list) else []
    preview = {"photo": photos[0] if photos else None, "address": tidy_caps(prop.street_address) or "", "city": tidy_caps(prop.city) or "",
               "price": prop.price, "beds": prop.bedrooms, "baths": prop.bathrooms, "sqft": prop.living_area,
               "brand_color": kit.get("secondary_color") or "#C9A24D", "photos": [x for x in photos if isinstance(x, str)][:16]}
    return Prepared(f"Make a Spotlight for {tidy_caps(prop.street_address)}",
                    ["Made from your listing's photos and details - I write the highlights, the story and the town section for you",
                     "Saved as a draft - nothing goes live until you publish"]
                    + (["You already have a Spotlight for this home - this makes another"] if have else []),
                    {"listing_key": key, "template_id": style, "preview": preview}, confirm_label="Make it", picker="spotlight")


SPOTLIGHT_SECTIONS = {"story", "gallery", "features", "video", "virtual_tour", "floor_plans", "details", "location", "explore_nearby",
                      "payment", "open_houses", "documents", "agents", "town", "guest_tools"}


def _adjust_spotlight(user, payload: dict, picks: dict) -> dict:
    out = dict(payload)
    if picks.get("template_id") in ("cinematic_luxury", "editorial_estate"):
        out["template_id"] = picks["template_id"]
    color = picks.get("brand_color")
    if color and color != (payload.get("preview") or {}).get("brand_color"):
        if not _HEX.match(str(color)):
            raise ActionError("That color isn't one I can use.")
        out["brand_color"] = str(color)   # a Spotlight wears the Branding color - this changes it
    # The editor's own choices, picked on the card (owner, 11/05) - saved to the draft once it's made.
    draft: dict = {}
    hero = picks.get("hero_image")
    if hero and hero in ((payload.get("preview") or {}).get("photos") or []):
        draft["hero_image"] = hero
    head = {}
    if picks.get("header_mode") in ("photo", "cycle"):
        head["mode"] = picks["header_mode"]
    if picks.get("header_motion") in ("out", "in", "both", "none"):
        head["motion"] = picks["header_motion"]
    if head:
        draft["header"] = head
    hidden = picks.get("hidden_sections")
    if isinstance(hidden, list):
        vis = {k: False for k in hidden if k in SPOTLIGHT_SECTIONS}
        if "open_houses" in vis:
            vis["open_houses_by_agent"] = True
        if vis:
            draft["section_visibility"] = vis
    tour = _s(picks.get("virtual_tour_url"))
    if tour:
        if not re.match(r"^https?://\S+$", tour):
            raise ActionError("That 3D tour link doesn't look right - it should start with https://")
        draft["virtual_tour_url"] = tour[:500]
    out["draft"] = draft
    out["story_mode"] = "original" if picks.get("story_mode") == "original" else "short"
    out["publish"] = picks.get("publish") is True
    out["editor"] = picks.get("editor") is True
    return out


async def _r_spotlight(db, user, p):
    if p.get("brand_color"):
        await _run_route(user, "PUT", "/studio/brand-kit", {"secondary_color": p["brand_color"]})
    out = await _run_route(user, "POST", "/studio/property-sites", {"listing_key": p["listing_key"], "template_id": p["template_id"]})
    sid = (out or {}).get("id")
    if not sid:
        return {"message": "Spotlight made.", "link": "/studio/property-sites"}
    if p.get("draft"):
        await _run_route(user, "PATCH", f"/studio/property-sites/{sid}", p["draft"])
    link = f"/studio/property-sites/{sid}"
    color_note = " Your brand color changed too." if p.get("brand_color") else ""
    if p.get("editor"):   # "Rather finish it in the full editor?": the editor does the first-open setup, in front of them
        return {"message": "Spotlight made." + color_note, "link": f"{link}?new=1" + ("&story=short" if p.get("story_mode") != "original" else "")}
    # The first-time setup runs on the server, so it finishes even if they close the site (11/05).
    import asyncio
    from app.services.ai import ai_notify, ai_spotlight_setup
    from app.services.ai.ai_actions import CARD
    await ai_notify.watch(db, user, f"spotlight:{sid}", CARD.get() or {}, (p.get("preview") or {}).get("address") or "this home")
    task = asyncio.create_task(ai_spotlight_setup.run(user.id, sid, p.get("story_mode") or "short", bool(p.get("publish"))))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return {"working": True, "message": "Writing your Spotlight now - the highlights, the story and the town. It takes about a minute, and I'll tell you right here when it's "
                                        "ready, even if you close the site." + color_note}

_simple("create_spotlight", "Make a Listing Spotlight (single-listing website) for one of the agent's own active listings (listing_key from search_properties). "
        "The card has a live preview and steps for the look (Luxe / Editorial), color, main photo, how the header moves, short or original story, which sections show, a 3D tour link, and draft or publish; "
        "then it writes the highlights, story and town itself on the server, so it finishes even if they leave. Don't ask the agent any of that or about the house. IDX plan.",
        _obj({"listing_key": {"type": "string"}, "style": {"type": "string", "enum": ["luxe", "editorial"]}}, ["listing_key"]), _p_spotlight, _r_spotlight, IDX,
        adjust=_adjust_spotlight)


async def _p_spotlight_pub(db, user, a):
    sid = _need_id(a, "spotlight_id")
    site = await _one(db, select(PropertyWebsite).where(PropertyWebsite.id == sid, PropertyWebsite.owner_id == user.id), "I couldn't find that Spotlight.")
    addr = tidy_caps((site.property_snapshot or {}).get("street_address") or (site.property_snapshot or {}).get("address") or "") or "this home"
    on = bool(a.get("publish"))
    return Prepared(f"{'Publish' if on else 'Take down'} the Spotlight for {addr}", ["Puts the page live for anyone with the link" if on else "The page goes offline; your work is kept"],
                    {"spotlight_id": sid, "publish": on}, confirm_label="Publish" if on else "Take it down")


async def _r_spotlight_pub(db, user, p):
    await _run_route(user, "POST", f"/studio/property-sites/{p['spotlight_id']}/{'publish' if p['publish'] else 'unpublish'}")
    share = None
    if p["publish"]:
        from app.services.property_website_service import public_url
        site = await db.get(PropertyWebsite, p["spotlight_id"])
        url = await public_url(db, site) if site else None
        share = {"url": url, "preview": _spotlight_preview(site, url)} if url else None
    return {"message": "Published." if p["publish"] else "Taken down.", "link": f"/studio/property-sites/{p['spotlight_id']}", "share": share}

def _spotlight_preview(site, url: str) -> str:
    """Where its preview is read: its own address, or its openhousepal.com one when it's on the
    agent's own domain (the preview reader only opens our own addresses)."""
    import os
    base = os.getenv("OHP_BASE_DOMAIN") or "openhousepal.com"
    return url if base in url else f"https://{site.slug}.{base}"

_simple("publish_spotlight", "Publish (publish=true) or take down (false) one of the agent's Listing Spotlights (spotlight_id from list_my_spotlights).",
        _obj({"spotlight_id": {"type": "string"}, "publish": {"type": "boolean"}}, ["spotlight_id", "publish"]), _p_spotlight_pub, _r_spotlight_pub, IDX)

_HEX = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


# The Branding card's swatches by name (frontend lib/accentSwatches.ts) - the card says "Navy", not "#1E3A5F"
BRAND_SWATCH_NAMES = {"#9B7659": "Bronze", "#C5C9CA": "Silver", "#D8B86A": "Gold", "#C9A24D": "Original gold", "#F1746B": "Light Red", "#EC4899": "Pink",
                      "#2563EB": "Blue", "#4A6741": "Green", "#7C3AED": "Purple", "#1E3A5F": "Navy", "#111827": "Charcoal", "#DC2626": "Red",
                      "#7F1D1D": "Burgundy", "#EA580C": "Orange", "#0F766E": "Teal", "#0284C7": "Sky Blue", "#166534": "Forest", "#7C4A2D": "Brown",
                      "#475569": "Slate", "#000000": "Black", "#FFFFFF": "White"}


BRAND_LOGOS = ("personal", "team", "brokerage")
BRAND_FONTS = ("serif", "sans", "mono")


async def _p_brand(db, user, a):
    body, lines = {}, []
    for k, label in (("primary_color", "Main color"), ("secondary_color", "Brand color"), ("accent_color", "Accent color")):
        if _s(a.get(k)):
            if not _HEX.match(_s(a[k])):
                raise ActionError("Colors need to be hex codes like #1F3A5F.")
            body[k] = _s(a[k]); lines.append(f"{label}: {BRAND_SWATCH_NAMES.get(body[k].upper(), body[k])}")
    if a.get("look") in ("classic", "luxe"):
        from app.utils.auth import luxe_theme_allowed
        if a["look"] == "luxe" and not luxe_theme_allowed(user):
            raise ActionError("The Luxe look comes with the IDX plan.")
        body["look"] = {"theme": a["look"]}; lines.append(f"Look: {a['look'].title()}")
    # No exact colors named: the card itself shows the Branding page's choices - look, color, Luxe
    # finish, Ivory/Midnight - starting from what they have now (owner, 10/31: "show me my options").
    kit = {}
    try:
        kit = await _run_route(user, "GET", "/studio/brand-kit") or {}
    except Exception:  # noqa: BLE001
        kit = {}
    look = kit.get("look") or {}
    current = {"theme": look.get("theme") or "classic", "metal": look.get("metal") or "gold", "dark": bool(look.get("dark")),
               "secondary_color": kit.get("secondary_color") or "#C9A24D", "idx": user.plan_tier == "IDX",
               "heading_font": kit.get("heading_font") or "serif", "body_font": kit.get("body_font") or "sans", "disclaimer_text": kit.get("disclaimer_text") or "",
               "logos": {k: kit.get(f"{k}_logo_url") for k in BRAND_LOGOS}, "headshot": user.photo_url}
    try:
        current["qr"] = await _run_route(user, "GET", "/me/qr-brand") or {}
    except ActionError:
        current["qr"] = {}
    # A photo dropped in the chat for this: it starts in the right spot on the card.
    if _s(a.get("logo_url")) and a.get("logo_kind") in BRAND_LOGOS:
        current["attached_logo"] = {"kind": a["logo_kind"], "url": _s(a["logo_url"])}
    if _s(a.get("headshot_url")):
        current["attached_headshot"] = _s(a["headshot_url"])
    # The tools that don't wear the brand right now - the card asks "use it on every tool?" like the
    # Branding page's "Use this color for all features" (owner, 11/01).
    try:
        u = await _run_route(user, "GET", "/studio/brand-kit/usage") or {}
    except Exception:  # noqa: BLE001
        u = {}
    off = []
    if (u.get("hub") or {}).get("exists") and not u["hub"].get("follows"): off.append("Agent Hub")
    if (u.get("signature") or {}).get("follows") is False: off.append("Email signature")
    fm = u.get("forms") or {}
    if (fm.get("total") or 0) > (fm.get("follows") or 0): off.append("Forms")
    if current["idx"] and (u.get("idx") or {}).get("exists") and not u["idx"].get("follows"): off.append("IDX website")
    iv = u.get("invites") or {}
    if (iv.get("total") or 0) > (iv.get("follows") or 0): off.append("Invites")
    current["not_using"] = off
    if not body:
        return Prepared("Your Branding", ["Pick your look, colors, logos, headshot and fonts - your hub, forms, signature and website wear them"],
                        {"body": {}, "current": current},
                        confirm_label="Save", picker="brand")
    return Prepared("Update your Branding", lines + ["Your hub, forms, signature and website wear it"], {"body": body, "current": current},
                    picker="brand")


def _adjust_brand(user, payload: dict, picks: dict) -> dict:
    """The swatches tapped on the card -> the Branding update (same rules as the Branding page)."""
    body = dict(payload.get("body") or {})
    look = dict(body.get("look") or {})
    if picks.get("theme") in ("classic", "luxe"):
        from app.utils.auth import luxe_theme_allowed
        if picks["theme"] == "luxe" and not luxe_theme_allowed(user):
            raise ActionError("The Luxe look comes with the IDX plan.")
        look["theme"] = picks["theme"]
    if picks.get("metal") in ("gold", "silver", "bronze"):
        look["metal"] = picks["metal"]
    if isinstance(picks.get("dark"), bool):
        look["dark"] = picks["dark"]
    if picks.get("secondary_color"):
        if not _HEX.match(str(picks["secondary_color"])):
            raise ActionError("That color isn't one I can use.")
        body["secondary_color"] = str(picks["secondary_color"])
    if look:
        body["look"] = look
    cur = payload.get("current") or {}
    for k in ("heading_font", "body_font"):
        if picks.get(k) in BRAND_FONTS and picks[k] != cur.get(k):
            body[k] = picks[k]
    if "disclaimer_text" in picks and _s(picks.get("disclaimer_text")) != _s(cur.get("disclaimer_text")):
        body["disclaimer_text"] = _s(picks.get("disclaimer_text"))[:2000]
    logos = {k: u for k, u in (picks.get("logos") or {}).items() if k in BRAND_LOGOS and isinstance(u, str) and u.startswith("http") and u != (cur.get("logos") or {}).get(k)}
    headshot = picks.get("headshot") if isinstance(picks.get("headshot"), str) and picks["headshot"].startswith("http") and picks["headshot"] != cur.get("headshot") else None
    qr = picks.get("qr") if picks.get("qr") in ("ohp", "logo", "initials") and picks.get("qr") != (cur.get("qr") or {}).get("choice") else None
    apply_all = bool(picks.get("apply_all"))
    if not body and not apply_all and not logos and not headshot and not qr:
        raise ActionError("Pick something to change first.")
    return {**payload, "body": body, "apply_all": apply_all, "logos": logos, "headshot": headshot, "qr": qr}


async def _r_brand(db, user, p):
    if not p.get("body") and not p.get("apply_all") and not p.get("logos") and not p.get("headshot") and not p.get("qr"):
        raise ActionError("Pick something to change first.")
    from app.services.ai import ai_attachments
    for kind, url in (p.get("logos") or {}).items():
        await ai_attachments.post_file(db, user, url, f"/studio/brand-kit/logo/{kind}", keep_png=True)
    if p.get("headshot"):
        await ai_attachments.post_file(db, user, p["headshot"], "/agent-hub/me/photo")
    if p.get("qr"):
        await _run_route(user, "PUT", "/me/qr-brand", {"choice": p["qr"]})
    # The same saves as the Branding page: the brand kit, then (if they said so) "Use this color for
    # all features" - so Branding, Settings and every tool show the same thing.
    if p.get("body"):
        await _run_route(user, "PUT", "/studio/brand-kit", p["body"])
    if p.get("apply_all"):
        await _run_route(user, "POST", "/studio/brand-kit/apply-all", {})
        return {"message": "Branding saved and every tool uses it now - your hub, forms, signature, invites and website.", "link": "/studio/brand-kit"}
    return {"message": "Branding saved - every tool set to your brand wears it now.", "link": "/studio/brand-kit"}

register({"name": "update_branding", "description": (
    "Change the agent's Branding (brand kit). For 'change my colors / show me my options / change my look / put my colors back / undo' with no color named, call it WITHOUT colors - "
    "the card shows every color, the Classic/Luxe look, the Luxe finish and Ivory/Midnight for them to tap, exactly like the Branding page. "
    "When they NAME a color, even in words ('navy', 'forest green', 'navy and gold'), pass it as a hex code in secondary_color (their brand color - "
    "the one the card and every tool use). Use the card's own swatches: navy #1E3A5F, gold #C9A24D (Original), light gold #D8B86A, bronze #9B7659, "
    "silver #C5C9CA, blue #2563EB, sky blue #0284C7, green #4A6741, forest #166534, teal #0F766E, purple #7C3AED, pink #EC4899, red #DC2626, "
    "light red #F1746B, burgundy #7F1D1D, orange #EA580C, brown #7C4A2D, slate #475569, charcoal #111827, black #000000) "
    "so the card starts on it; a second named color goes in accent_color. Never tell them to pick a color they already named. "
    "The card also takes their logos, headshot, fonts, disclaimer and QR center. A logo they attached goes in logo_url with logo_kind (personal, team or brokerage); a headshot in headshot_url."),
    "input_schema": _obj({"primary_color": {"type": "string"}, "secondary_color": {"type": "string"}, "accent_color": {"type": "string"}, "look": {"type": "string", "enum": ["classic", "luxe"]},
                          "logo_url": {"type": "string"}, "logo_kind": {"type": "string", "enum": ["personal", "team", "brokerage"]}, "headshot_url": {"type": "string"}})},
    _p_brand, _r_brand, None, adjust=_adjust_brand)


Q_TYPES = {"short_text": "Short answer", "long_text": "Long answer", "select": "Pick one", "multi_select": "Pick any",
           "number": "Number", "currency": "Dollar amount", "date": "Date", "time": "Time", "email": "Email", "phone": "Phone", "contact_group": "Several on one page"}
GROUP_FIELD_TYPES = ("short_text", "email", "phone", "number", "currency", "date")

# Questions an agent's forms usually ask - tap to add them on the card (owner, 11/01: "buttons with
# steps ... instead of the agent typing everything").
FORM_QUESTION_BANK = [
    {"label": "When are you hoping to move?", "type": "select", "options": ["As soon as possible", "1-3 months", "3-6 months", "6+ months", "Just looking"]},
    {"label": "Are you working with an agent?", "type": "select", "options": ["Yes", "No"]},
    {"label": "Are you pre-approved for a mortgage?", "type": "select", "options": ["Yes", "Not yet", "Paying cash"]},
    {"label": "What price range are you looking in?", "type": "short_text"},
    {"label": "How many bedrooms do you need?", "type": "select", "options": ["1", "2", "3", "4", "5+"]},
    {"label": "Do you have a home to sell first?", "type": "select", "options": ["Yes", "No"]},
    {"label": "What's the address of your home?", "type": "short_text"},
    {"label": "Anything else I should know?", "type": "long_text"},
]

# The card's "Common ones" follow what the form is for (owner, 11/01: "not good enough suggestion wise
# based on the situation"); the model's own `ideas` for this exact form come first on the card.
FORM_BANKS = {
    "seller": [
        {"label": "What's the address of your home?", "type": "short_text"},
        {"label": "When are you thinking of selling?", "type": "select", "options": ["As soon as possible", "1-3 months", "3-6 months", "6+ months", "Just curious"]},
        {"label": "Have you made any updates to the home?", "type": "long_text"},
        {"label": "Do you need to buy your next home first?", "type": "select", "options": ["Yes", "No", "Not sure yet"]},
        {"label": "What price are you hoping for?", "type": "currency"},
        {"label": "Are you working with an agent?", "type": "select", "options": ["Yes", "No"]},
    ],
    "buyer": [
        {"label": "When are you hoping to move?", "type": "select", "options": ["As soon as possible", "1-3 months", "3-6 months", "6+ months", "Just looking"]},
        {"label": "What price range are you looking in?", "type": "select", "options": ["Under $400K", "$400K-$700K", "$700K-$1M", "$1M-$2M", "$2M+"]},
        {"label": "Are you pre-approved for a mortgage?", "type": "select", "options": ["Yes", "Not yet", "Paying cash"]},
        {"label": "How many bedrooms do you need?", "type": "select", "options": ["1", "2", "3", "4", "5+"]},
        {"label": "Which towns are you looking in?", "type": "short_text"},
        {"label": "Do you have a home to sell first?", "type": "select", "options": ["Yes", "No"]},
        {"label": "Are you working with an agent?", "type": "select", "options": ["Yes", "No"]},
    ],
    "event": [
        {"label": "Will you be attending?", "type": "select", "options": ["Yes", "No", "Maybe"]},
        {"label": "How many people are coming with you?", "type": "select", "options": ["Just me", "+1", "+2", "+3 or more"]},
        {"label": "Any food allergies or needs?", "type": "short_text"},
        {"label": "How did you hear about this?", "type": "select", "options": ["Email", "Social media", "A friend", "Other"]},
    ],
    "feature": [
        {"label": "Tell me a little about your home", "type": "long_text"},
        {"label": "What's the address?", "type": "short_text"},
        {"label": "What's the best time to reach you?", "type": "select", "options": ["Morning", "Afternoon", "Evening"]},
        {"label": "Can you share a few photos?", "type": "select", "options": ["Yes", "Not right now"]},
    ],
    "rental": [
        {"label": "When do you need to move in?", "type": "date"},
        {"label": "What's your monthly budget?", "type": "currency"},
        {"label": "How many people will live there?", "type": "select", "options": ["1", "2", "3", "4+"]},
        {"label": "Do you have pets?", "type": "select", "options": ["No", "Dog", "Cat", "Other"]},
    ],
}


def _form_bank(title: str) -> list:
    t = (title or "").lower()
    kind = ("seller" if any(w in t for w in ("sell", "seller", "listing", "home value", "valuation", "what's my home", "worth")) else
            "rental" if any(w in t for w in ("rent", "lease", "tenant")) else
            "event" if any(w in t for w in ("event", "rsvp", "party", "invite", "seminar", "workshop", "class", "dinner")) else
            "feature" if any(w in t for w in ("feature", "show", "youtube", "podcast", "spotlight", "story", "kitchen", "tour my")) else
            "buyer" if any(w in t for w in ("buy", "buyer", "search", "looking", "first-time", "relocat")) else "")
    bank = FORM_BANKS.get(kind, [])
    seen = {b["label"].lower() for b in bank}
    return bank + [b for b in FORM_QUESTION_BANK if b["label"].lower() not in seen]


def _safe_form_questions(raw) -> list:
    """Like _form_questions, but a bad idea is skipped instead of stopping the card."""
    out = []
    for q in (raw if isinstance(raw, list) else [raw] if raw else []):
        try:
            out.extend(_form_questions([q])[0])
        except ActionError:
            continue
    return out


def _q_extras(q: dict, qt: str) -> dict:
    """What a question can carry beyond its words: its fork, and the questions sharing its page."""
    out = {}
    if qt == "contact_group":
        import uuid
        fields = [{"id": _s(f.get("id")) or f"f_{uuid.uuid4().hex[:8]}", "type": f.get("type") if f.get("type") in GROUP_FIELD_TYPES else "short_text",
                   "label": _s(f.get("label"))[:120], "required": bool(f.get("required"))}
                  for f in (q.get("fields") or []) if isinstance(f, dict) and _s(f.get("label"))][:6]
        if len(fields) >= 2:
            out["fields"] = fields
    s = q.get("show_if")
    if isinstance(s, dict) and _s(s.get("question")) and isinstance(s.get("answers"), list) and s["answers"]:
        out["show_if"] = {"question": _s(s["question"]), "answers": [str(a).strip() for a in s["answers"]][:12]}
    return out


def _with_forks(qs: list) -> list:
    """The card's questions as the form stores them: each show_if becomes depends_on, pointing at the
    real id of the earlier question and its exact answer words. One it can't place is asked of everyone."""
    norm = lambda v: str(v or "").strip().lower()  # noqa: E731
    seen, out = {}, []
    for q in qs:
        row = {k: v for k, v in q.items() if k not in ("show_if", "depends_on")}
        fork = q.get("show_if") or {}
        target = seen.get(norm(fork.get("question")))
        if target:
            opts = {norm(o): o for o in target.get("options") or []}
            vals = [opts[norm(a)] for a in fork.get("answers") or [] if norm(a) in opts]
            if vals:
                row["depends_on"] = {"question_id": target["id"], "values": vals}
        seen[norm(q.get("label"))] = row
        out.append(row)
    return out


def _form_questions(raw) -> tuple[list, list]:
    """Questions as the model or the card sends them -> the form's questions + the card's lines."""
    from app.services.form_question_heal import looks_like_blob, parse_blob
    qs, lines = [], []
    if isinstance(raw, str):   # the model sometimes sends the list as JSON text
        # (whole or cut off - 11/01 a text that didn't parse became one question showing raw JSON)
        raw = parse_blob(raw) if looks_like_blob(raw) else [ln for ln in raw.splitlines() if ln.strip()]
    if not isinstance(raw, list):
        raw = []
    flat = []
    for q in raw:   # ...or one item of the list is itself JSON text
        flat.extend((parse_blob(q) or []) if isinstance(q, str) and looks_like_blob(q) else [q])
    for i, q in enumerate(flat[:15]):
        # ...or a question as just its words - a short answer
        q = {"label": q} if isinstance(q, str) else q if isinstance(q, dict) else {}
        label, qt = _s(q.get("label")), _s(q.get("type")) or "short_text"
        if not label:
            continue
        if qt not in Q_TYPES:
            qt = "short_text"
        opts = [str(o).strip()[:80] for o in (q.get("options") or []) if str(o).strip()][:20]
        if qt in ("select", "multi_select") and len(opts) < 2:
            raise ActionError(f"\u201c{label}\u201d needs at least two answers to pick from.")
        extras = _q_extras(q, qt)
        if qt == "contact_group" and "fields" not in extras:
            qt = "short_text"
        qs.append({"id": f"ai_q{i + 1}", "type": qt, "label": label[:200], "required": bool(q.get("required")),
                   **({"options": opts} if qt in ("select", "multi_select") else {}), **extras})
        lines.append(f"{label} ({Q_TYPES[qt]}{': ' + ', '.join(opts) if opts else ''}){' - required' if q.get('required') else ''}")
    return qs, lines


async def _form_homes(db, user) -> list:
    """Their own active listings a form can link to."""
    from app.utils.plan_ids import listing_owner_key
    mine = listing_owner_key(user)
    if not mine:
        return []
    rows = (await db.execute(select(Property).where(
        or_(Property.list_agent_mls_id == mine, Property.co_list_agent_mls_id == mine),
        Property.home_status.in_(("ACTIVE-BRIGHT", "COMING SOON-BRIGHT"))).order_by(Property.mls_list_date.desc().nulls_last()).limit(6))).scalars().all()
    # price / beds / baths / sq ft too: the card's preview draws the form's home card from these
    return [{"listing_key": r.listing_key, "address": tidy_caps(r.street_address) or "", "city": tidy_caps(r.city) or "", "state": r.state,
             "price": r.price, "beds": r.bedrooms, "baths": r.bathrooms, "sqft": r.living_area, "status": r.home_status,
             "photo": (r.photos[0] if isinstance(r.photos, list) and r.photos and isinstance(r.photos[0], str) else None)} for r in rows]


async def _p_form(db, user, a):
    title = _s(a.get("title"))
    if not title:
        raise ActionError("What should the form be called?")
    qs, _qlines = _form_questions(a.get("questions") or [])
    one = a.get("one_contact_screen") is not False   # owner, 11/04: one screen, first and last on one line
    split = one and a.get("first_last_name") is not False
    lines = [f"Starts with {'First and last name' if split else 'Full name'}, Email and Phone" + (" on one screen" if one else "")]   # the questions themselves are a step on the card
    # The two look choices are taps on the card, not chat questions (owner, 11/01: "buttons, much less
    # words"): moving background on/off, and one of their own active listings to link (or none).
    homes = await _form_homes(db, user)
    key = _s(a.get("listing_key"))
    style_arg = _style_arg(user, a)
    ideas = [q for q in _safe_form_questions(a.get("ideas") or []) if q["label"].lower() not in {x["label"].lower() for x in qs}][:6]
    payload = {"title": title[:120], "questions": qs, "homes": homes, "ideas": ideas, "bank": _form_bank(title),
               "background_effects": a["background_effects"] if isinstance(a.get("background_effects"), bool) else True,
               "welcome_text_effects": a["welcome_text_effects"] if isinstance(a.get("welcome_text_effects"), bool) else True,
               "dark": a["dark_mode"] if isinstance(a.get("dark_mode"), bool) else None,
               "one_contact": one, "first_last": split, **style_arg,
               **_after_arg(a.get("after_submit")), "told": _told(a),
               "listing_key": key if any(h["listing_key"] == key for h in homes) or not key else key}
    return Prepared(f"Make a new form: {title}", lines + ["You can change the look and add more in the form editor"], payload, picker="form")


def _adjust_form(user, payload: dict, picks: dict) -> dict:
    out = dict(payload)
    if isinstance(picks.get("questions"), list):
        out["questions"], _ = _form_questions(picks["questions"])
    for k in ("background_effects", "welcome_text_effects", "one_contact", "first_last"):
        if isinstance(picks.get(k), bool):
            out[k] = picks[k]
    if "after_mode" in picks:
        out["after_mode"], out["after_url"] = _s(picks.get("after_mode")) or None, _s(picks.get("after_url")) or None
        if out["after_mode"]:
            from fastapi import HTTPException
            from app.services.forms_service import normalize_form_redirect
            try:
                normalize_form_redirect(out["after_mode"], out["after_url"])
            except HTTPException as e:
                raise ActionError(str(e.detail))
    if "look" in picks:
        out["look"] = picks["look"] if isinstance(picks["look"], dict) else None
    if "style" in picks:
        out["style"] = picks["style"] if isinstance(picks["style"], dict) else None
    if "dark" in picks:
        out["dark"] = picks["dark"] if isinstance(picks["dark"], bool) else None
    if "listing_key" in picks:
        out["listing_key"] = _s(picks.get("listing_key")) or None
    return out


# The form look's Classic colors, by the name people use (lib/accentSwatches, minus the metals).
FORM_COLORS = {"original": "#C9A24D", "light red": "#F1746B", "pink": "#EC4899", "blue": "#2563EB", "green": "#4A6741", "purple": "#7C3AED", "navy": "#1E3A5F",
               "charcoal": "#111827", "red": "#DC2626", "burgundy": "#7F1D1D", "orange": "#EA580C", "teal": "#0F766E", "sky blue": "#0284C7", "forest": "#166534",
               "brown": "#7C4A2D", "slate": "#475569", "black": "#000000"}


def form_style(user, look=None, color=None, metal=None) -> dict:
    """What they named of a form's look -> the card's style pick. Luxe is the IDX plan's, like the editor."""
    style = {}
    metal, color = _s(metal).lower(), FORM_COLORS.get(_s(color).lower())
    if metal in ("gold", "silver", "bronze"):
        style.update(theme="luxe", metal=metal)
    if color and not metal:
        style.update(theme="classic", accent=color)
    if look in ("classic", "luxe"):
        style["theme"] = look
    if style.get("theme") == "luxe" and user.plan_tier != "IDX" and not user.is_admin:
        raise ActionError("The Luxe look comes with the IDX plan.")
    return style


def _style_arg(user, a: dict) -> dict:
    style = form_style(user, a.get("look"), a.get("color"), a.get("metal"))
    return {"style": style} if style else {}


CONTACT_IDS = ["full_name", "email", "phone"]
SPLIT_IDS = ["buyer_first_name", "buyer_last_name", "email", "phone"]


def _told(a: dict) -> list:
    """Card settings the helper set on purpose - the rest carry over from the card this one replaces."""
    return [k for k, arg in (("one_contact", "one_contact_screen"), ("first_last", "first_last_name"), ("background_effects", "background_effects"),
                             ("welcome_text_effects", "welcome_text_effects"), ("dark", "dark_mode"), ("after_mode", "after_submit"), ("listing_key", "listing_key")) if a.get(arg) is not None] + (
        ["style"] if a.get("look") or a.get("color") or a.get("metal") else [])


def _after_arg(v) -> dict:
    """The helper's after_submit: a choice, or a web address."""
    v = _s(v)
    if not v:
        return {}
    return {"after_mode": v, "after_url": None} if v in ("agent_hub", "website", "property", "none") else {"after_mode": "custom", "after_url": v}


def _after_update(p: dict) -> dict:
    """Where the thank-you sends them. A form about a home goes to that home unless they picked."""
    mode = _s(p.get("after_mode")) or ("property" if _s(p.get("listing_key")) else "")
    if not mode:
        return {}
    return {"redirect_mode": mode, "redirect_url": p.get("after_url") if mode == "custom" else None,
            **({"redirect_to_agent_hub": mode != "none"})}


def _contact_block(locked: list, one: bool, split: bool = False) -> list:
    """Name, email and phone as three screens or one, the same one-screen question the premade forms
    use. Field ids stay the same either way, so leads and past answers read the same (owner, 11/04)."""
    ids = [q.get("id") for q in locked]
    grouped = ids == ["contact_info"] and [f.get("id") for f in locked[0].get("fields") or []] in (CONTACT_IDS, SPLIT_IDS)
    if not (ids == CONTACT_IDS or grouped):
        return locked
    name = ([{"id": "buyer_first_name", "type": "short_text", "label": "First name", "required": True}, {"id": "buyer_last_name", "type": "short_text", "label": "Last name", "required": True}]
            if split and one else [{"id": "full_name", "type": "short_text", "label": "Full name", "required": True}])
    fields = name + [{"id": "email", "type": "email", "label": "Email", "required": True}, {"id": "phone", "type": "phone", "label": "Phone", "required": True}]
    if one:
        return [{"id": "contact_info", "type": "contact_group", "label": "Your Contact Information", "required": True, "locked": True, "fields": fields}]
    return [{**f, "locked": True} for f in fields]


def _look_update(p: dict) -> dict:
    """The card's Light / Dark pick: the form takes its own look, the rest copied from the brand look
    so only light or dark changes - what the form editor's own picker does."""
    look = p.get("look")
    if not isinstance(look, dict) or not isinstance(look.get("dark"), bool):
        return {}
    return {"look_mode": "custom", "dark_mode": look["dark"], "look_theme": look.get("theme") if look.get("theme") in ("classic", "luxe") else "classic",
            **({"accent_color": look["accent"]} if re.fullmatch(r"#[0-9a-fA-F]{6}", str(look.get("accent") or "")) else {}),
            **({"luxe_metal": look["metal"]} if look.get("metal") in ("gold", "silver", "bronze") else {})}


async def _linked_home(db, key: str) -> dict:
    prop = await _one(db, select(Property).where(Property.listing_key == key), "I couldn't find that home in the listings.")
    photos = prop.photos if isinstance(getattr(prop, "photos", None), list) else []
    return {k: v for k, v in {
        "listing_key": key, "address": prop.street_address, "city": prop.city, "state": prop.state, "zipcode": getattr(prop, "zipcode", None),
        "price": prop.price, "beds": getattr(prop, "bedrooms", None), "baths": getattr(prop, "bathrooms", None), "sqft": getattr(prop, "living_area", None),
        "image_url": photos[0] if photos and isinstance(photos[0], str) else None}.items() if v is not None}


async def _r_form(db, user, p):
    if _s(p.get("listing_key")):
        p = {**p, "linked_property": await _linked_home(db, p["listing_key"])}
    out = await _run_route(user, "POST", "/agent-hub/forms", {"title": p["title"]})
    fid = (out or {}).get("id")
    update = {**{k: p[k] for k in ("background_effects", "welcome_text_effects", "linked_property") if k in p}, **_look_update(p), **_after_update(p)}
    if fid and (p.get("questions") or p.get("one_contact")):
        update["questions"] = _contact_block(list((out or {}).get("questions") or []), bool(p.get("one_contact")), bool(p.get("first_last"))) + _with_forks(list(p.get("questions") or []))
    if fid and update:
        await _run_route(user, "PUT", f"/agent-hub/forms/{fid}", update)
    share = None
    if (out or {}).get("slug"):
        from app.services.website.site_service import owner_links
        links = await owner_links(db, user)
        if links.hub_slug:
            share = await _share(db, user, f"/{links.hub_slug}/{out['slug']}", url=links.form(out["slug"]))
    return {"message": "Form made. In its editor you can also change the look and colors, the welcome text, the thank-you page and where it sends people after.",
            "link": f"/studio/forms?edit={fid}" if fid else "/studio/forms", "share": share}

_FORM_Q = {"type": "array", "items": {"type": "object", "properties": {
    "label": {"type": "string"}, "type": {"type": "string", "enum": list(Q_TYPES)}, "options": {"type": "array", "items": {"type": "string"}}, "required": {"type": "boolean"},
    "show_if": {"type": "object", "description": "A fork: ask this only when that earlier pick-one question got one of these answers", "properties": {"question": {"type": "string"}, "answers": {"type": "array", "items": {"type": "string"}}}},
    "fields": {"type": "array", "description": "contact_group only: the questions sharing this page (text, email, phone, number, currency, date)", "items": {"type": "object", "properties": {"label": {"type": "string"}, "type": {"type": "string"}}}}}}}
_FORM_EXTRA = {"first_last_name": {"type": "boolean", "description": "Forms start with first and last name side by side on one line. false only if they ask for one Full name box"},
               "look": {"type": "string", "enum": ["classic", "luxe"], "description": "Only if they name the style"},
               "color": {"type": "string", "enum": [n.title() for n in FORM_COLORS], "description": "Only if they name a Classic color"},
               "metal": {"type": "string", "enum": ["gold", "silver", "bronze"], "description": "Only if they name a Luxe finish"},
               "after_submit": {"type": "string", "description": "Only if they name where people go after the thank-you: agent_hub, website, property, none, or a web address. \"Finish the form\" is a fork, not this"}}

_simple("create_form", "Make a NEW custom form (to change one that exists use edit_form). A title, 3-5 questions that fit it (types: " + ", ".join(Q_TYPES) + "; select/multi_select need options; name, email and phone are already there) and `ideas`: 4-6 more to tap in. Real lists, never text. listing_key only if they named the home. "
        "The card handles the look, the linked home, where the thank-you goes and adding or removing questions - don't ask about those in chat. "
        "Forks only when the flow really needs one (they asked, or a question makes no sense for some answers): make that question pick-one and put show_if on each question for that path; \"if yes, finish\" means the No question gets show_if and no un-forked question follows. Several on one page: type contact_group with fields. "
        "While the card waits, any change goes through edit_form, never create_form again. In chat never number the questions your own way.",
        _obj({"title": {"type": "string"}, "questions": _FORM_Q,
            "ideas": {"type": "array", "items": {"type": "object", "properties": {
            "label": {"type": "string"}, "type": {"type": "string", "enum": list(Q_TYPES)}, "options": {"type": "array", "items": {"type": "string"}}}}},
            "background_effects": {"type": "boolean"}, "welcome_text_effects": {"type": "boolean"}, "dark_mode": {"type": "boolean"},
            "one_contact_screen": {"type": "boolean", "description": "Forms start with name, email and phone on ONE screen. false only if they ask for separate screens"}, **_FORM_EXTRA,
            "listing_key": {"type": "string"}},
             ["title"]), _p_form, _r_form, adjust=_adjust_form)


# Changing a form that already exists, from chat (owner, 11/04: after "Open in Forms" the chat could
# only make a new one). Same card as making one. The card keeps a copy of the questions it started
# from, so if the form is changed in its editor before they tap, it stops instead of overwriting
# (ai_actions._field_snapshot). Each question keeps its own id, so past answers stay attached.
async def _own_form(db, user, a):
    from app.models.database import AgentForm
    rows = (await db.execute(select(AgentForm).where(AgentForm.owner_id == user.id)
                             .order_by(AgentForm.updated_at.desc().nulls_last(), AgentForm.created_at.desc()))).scalars().all()
    fid, title = _s(a.get("form_id")), _s(a.get("title")).lower()
    hit = next((f for f in rows if f.id == fid), None) if fid else None
    if not hit and title:
        hit = (next((f for f in rows if (f.title or "").strip().lower() == title), None)
               or next((f for f in rows if title in (f.title or "").lower()), None))
    if not hit:
        # The model's words around the name: "Edit your form: Buyer Check" (a card's title), the name
        # sent as form_id, or only the change itself. Longest name first, so "Buyer" can't beat "Buyer Check".
        said = " ".join((title, fid.lower(), _s(a.get("change")).lower()))
        named = sorted((f for f in rows if (f.title or "").strip()), key=lambda f: -len(f.title))
        hit = next((f for f in named if f.title.strip().lower() in said), None)
    if not hit:
        logger.info("edit_form found no form: id=%r title=%r", fid, title)
        raise ActionError("I couldn't find that form. Which one do you mean?")
    return hit


def _merge_form_questions(existing: list, new: list) -> list:
    """The card's questions on top of the form's real ones: same words (or the same spot and kind,
    for a reworded one) keeps that question's id and everything else about it."""
    norm = lambda q: str(q.get("label") or "").strip().lower()  # noqa: E731
    by_label = {norm(q): q for q in existing}
    wanted = {norm(q) for q in new}
    used, out = set(), []
    for i, q in enumerate(new):
        old = by_label.get(norm(q))
        if old is None or old.get("id") in used:
            spot = existing[i] if i < len(existing) else None
            old = spot if spot and spot.get("id") not in used and spot.get("type") == q.get("type") and norm(spot) not in wanted else None
        if old:
            used.add(old.get("id"))
            kept = {k: v for k, v in old.items() if k not in ("options", "fields", "depends_on")}
            row = {**kept, **{k: q[k] for k in ("label", "type", "required", "options", "show_if") if k in q}}
            if q.get("fields"):   # a field keeps its id too, so past answers stay with it
                ids = {str(f.get("label")).strip().lower(): f.get("id") for f in old.get("fields") or []}
                row["fields"] = [{**f, "id": ids.get(f["label"].strip().lower()) or f["id"]} for f in q["fields"]]
            out.append(row)
        else:
            import uuid
            out.append({**q, "id": f"ai_{uuid.uuid4().hex[:8]}"})
    return out


async def _waiting_form_cards(db, user) -> list:
    rows = (await db.execute(select(AIMessage).join(AIConversation, AIConversation.id == AIMessage.conversation_id)
                             .where(AIConversation.owner_id == user.id, AIMessage.role == "assistant")
                             .order_by(AIMessage.created_at.desc()).limit(12))).scalars().all()
    return [c for m in rows for c in (m.tool_calls or []) if isinstance(c, dict) and c.get("tool") in ("create_form", "edit_form") and c.get("status") == "pending"]


async def _revise_card(db, user, title: str, questions: list, change: str) -> dict:
    """The change applied to the real list by the small editor the card's own change line uses: the
    chat never retypes a form, so it can't drop a fork or a page, and retyping cost more (owner, 11/04)."""
    from fastapi import HTTPException
    from app.services.ai import ai_copywrite_service
    try:
        out = await ai_copywrite_service.revise_form_questions(db, user.id, user.plan_tier, title=title or "Form", questions=questions, at=None, instruction=change)
    except HTTPException as e:
        raise ActionError(str(e.detail))
    await db.refresh(user)
    if out.get("changed") is False:
        raise ActionError(out["note"])
    style = form_style(user, out.pop("look", None), out.pop("color", None), out.pop("metal", None))
    return {**out, **({"style": style} if style else {})}


CARD_REVISED = ("one_contact", "first_last", "style", "dark")   # settings the card's change line can set


async def _p_edit_waiting(db, user, a, card: dict):
    """A form not made yet: the change goes on its waiting card, and that stays a card for a new form."""
    p, draft = dict(card.get("payload") or {}), card.get("draft_picks") if isinstance(card.get("draft_picks"), dict) else {}
    start = draft.get("questions") or p.get("questions") or []
    change = _s(a.get("change"))
    out = await _revise_card(db, user, p.get("title"), start, change) if change else {"questions": start}
    qs, _ = _form_questions(out["questions"])
    told = [k for k in CARD_REVISED if k in out] + (["after_mode"] if a.get("after_submit") else [])
    return Prepared(card.get("title") or f"Make a new form: {p.get('title')}", list(card.get("details") or []),
                    {**p, "questions": qs, **{k: out[k] for k in CARD_REVISED if k in out}, **_style_arg(user, a), **_after_arg(a.get("after_submit")), "told": told + (["style"] if _style_arg(user, a) else [])}, picker="form")


def _card_fits(q: dict, own: list) -> bool:
    if q.get("type") not in Q_TYPES:
        return False
    if q.get("type") == "contact_group" and any(f.get("type") not in GROUP_FIELD_TYPES for f in q.get("fields") or []):
        return False
    dep = (q.get("depends_on") or {}).get("question_id")
    return not dep or any(o.get("id") == dep and _card_fits(o, own) for o in own)


async def _p_edit_form(db, user, a):
    cards = await _waiting_form_cards(db, user)
    named, fid = _s(a.get("title")).lower(), _s(a.get("form_id"))
    new_card = next((c for c in cards if not (c.get("payload") or {}).get("form_id") and (not fid) and (not named or str((c.get("payload") or {}).get("title") or "").strip().lower() == named)), None)
    if new_card:
        return await _p_edit_waiting(db, user, a, new_card)
    form = await _own_form(db, user, a)
    if form.form_type == "showcase_request":
        raise ActionError("That form's questions are set - only its look can change, in the form editor.")
    own = [q for q in (form.questions or []) if isinstance(q, dict) and not q.get("locked")]
    # special = what a card can't hold: a kind it doesn't know, a page with a pick-one in it, or a fork
    # on a question it doesn't show. Those stay exactly as they are.
    plain = [q for q in own if _card_fits(q, own)]
    if len(plain) > 15:
        raise ActionError("That form has more questions than a chat card can hold - change it in the form editor.")
    labels = {q.get("id"): q.get("label") for q in plain}
    start = [{**{k: q.get(k) for k in ("label", "type", "options", "required", "fields") if q.get(k) is not None},
              **({"show_if": {"question": labels[q["depends_on"]["question_id"]], "answers": q["depends_on"]["values"]}} if q.get("depends_on") else {})} for q in plain]
    # A card still waiting for this form: this change goes on top of it, not on the saved form.
    waiting = next((c for c in cards if (c.get("payload") or {}).get("form_id") == form.id), None)
    if waiting:
        start = (waiting.get("draft_picks") or {}).get("questions") or (waiting.get("payload") or {}).get("questions") or start
    revised, change = {}, _s(a.get("change"))
    if change:
        revised = await _revise_card(db, user, form.title, start, change)
        await db.refresh(form)
        start = revised["questions"]
    qs, _ = _form_questions(start)
    title = form.title or "Your form"
    lines = ["Changes the form that's already live - answers people already sent stay"]
    if revised.get("note"):
        lines.insert(0, revised["note"])
    if len(own) > len(plain):
        lines.append(f"{len(own) - len(plain)} special question{'s stay' if len(own) - len(plain) != 1 else ' stays'} as is - those change in the form editor")
    ideas = [q for q in _safe_form_questions(a.get("ideas") or []) if q["label"].lower() not in {x["label"].lower() for x in qs}][:6]
    linked = form.linked_property if isinstance(form.linked_property, dict) else {}
    locked = [q for q in (form.questions or []) if isinstance(q, dict) and q.get("locked")]
    grouped = [q.get("id") for q in locked] == ["contact_info"] and _contact_block(locked, False) != locked      # on one screen, and can come apart
    fixed = not grouped and [q.get("id") for q in locked] != CONTACT_IDS   # a premade form's own contact questions
    payload = {"form_id": form.id, "title": title[:120], "questions": qs, "original": plain, "homes": await _form_homes(db, user), "ideas": ideas, "bank": _form_bank(title),
               "background_effects": form.background_effects is not False, "welcome_text_effects": form.welcome_text_effects is not False,
               "dark": revised["dark"] if isinstance(revised.get("dark"), bool) else bool(form.dark_mode) if form.look_mode == "custom" else None,
               "style": revised.get("style") or _style_arg(user, a).get("style") or ({"theme": form.look_theme or "classic", **({"accent": form.accent_color} if form.accent_color else {}), "metal": form.luxe_metal or "gold"} if form.look_mode == "custom" else None),
               "had_custom": form.look_mode == "custom",
               "one_contact": revised["one_contact"] if "one_contact" in revised and not fixed else grouped, "contact_fixed": fixed,
               "first_last": revised["first_last"] if "first_last" in revised and not fixed else any(f.get("id") == "buyer_first_name" for q in locked for f in q.get("fields") or []),
               "after_mode": form.redirect_mode, "after_url": form.redirect_url, **_after_arg(a.get("after_submit")), "told": _told(a) + [k for k in CARD_REVISED if k in revised],
               "listing_key": linked.get("listing_key") or None, "had_listing": linked.get("listing_key") or None,
               "body": {"questions": form.questions or []}}
    return Prepared(f"Edit your form: {title}", lines, payload, confirm_label="Save changes", picker="form")


async def _r_edit_form(db, user, p):
    if not p.get("form_id"):   # it was a new form's card all along
        return await _r_form(db, user, p)
    form = await _own_form(db, user, {"form_id": p.get("form_id")})
    now = [q for q in (form.questions or []) if isinstance(q, dict)]
    locked = [q for q in now if q.get("locked")]
    own = [q for q in now if not q.get("locked")]
    special = [q for q in own if not _card_fits(q, own)]
    update = {"questions": _contact_block(locked, bool(p.get("one_contact")), bool(p.get("first_last"))) + _with_forks(_merge_form_questions(p.get("original") or [], p.get("questions") or [])) + special,
              **(_look_update(p) or ({"look_mode": "brand"} if p.get("had_custom") else {})), **({"redirect_mode": p["after_mode"], "redirect_url": p.get("after_url") if p["after_mode"] == "custom" else None, "redirect_to_agent_hub": p["after_mode"] != "none"} if p.get("after_mode") else {})}
    for k in ("background_effects", "welcome_text_effects"):
        if isinstance(p.get(k), bool):
            update[k] = p[k]
    key = _s(p.get("listing_key"))
    if key and key != p.get("had_listing"):
        update["linked_property"] = await _linked_home(db, key)
    elif not key and p.get("had_listing"):
        update["linked_property"] = None
    await _run_route(user, "PUT", f"/agent-hub/forms/{form.id}", update)
    share = None
    from app.services.website.site_service import owner_links
    links = await owner_links(db, user)
    if links.hub_slug and form.slug:
        share = await _share(db, user, f"/{links.hub_slug}/{form.slug}", url=links.form(form.slug))
    return {"message": "Form updated. If you have its editor open in another tab, it picks up this version when you go back to it.",
            "link": f"/studio/forms?edit={form.id}", "share": share}

_simple("edit_form", "Change a form: one on a card still waiting, or one that exists (made here, opened in the editor, any of theirs) - never create_form for that. Pass its exact title or form_id (list_my_forms), and `change`: what they asked for in their words, forks, pages and combining the contact info included. Don't retype the questions or ask how to word them - the card's editor writes them. "
        "No `change` just opens the card. Another change while the card waits: call again with only that change. Past answers are kept.",
        _obj({"form_id": {"type": "string"}, "title": {"type": "string"}, "change": {"type": "string"}, "after_submit": _FORM_EXTRA["after_submit"]}, []),
        _p_edit_form, _r_edit_form, adjust=_adjust_form)

INVITE_TYPES = {"open_house": "Open house", "brokers_open": "Broker's open", "client_event": "Client event", "custom": "Other event"}
INVITE_DESIGNS = {"classic": "Classic", "garden": "Garden", "midnight": "Midnight", "modern": "Modern"}


async def _p_invite(db, user, a):
    # Set up at once - the card asks the rest as taps with a live preview (owner, 11/01: "buttons with
    # steps ... instead of so much text"): kind, which of their homes, date, times, design and color.
    kind = _s(a.get("kind")) if _s(a.get("kind")) in INVITE_TYPES else ""
    title = _s(a.get("title"))
    body = {"invite_type": kind or "client_event", "title": title[:140], "design": a.get("design") if a.get("design") in INVITE_DESIGNS else "classic"}
    lines = []
    if _s(a.get("date")):
        body["event_date"] = _date_ok(a["date"])
        for k in ("start_time", "end_time"):
            if _s(a.get(k)):
                body[k] = _time_ok(a[k])
    # A place NAME is a venue ("Merion Cricket Club"), never the street again - the chat once wrote
    # "239 Windham Dr" there and the invite showed the street twice (owner, 11/01).
    norm = lambda v: re.sub(r"[^a-z0-9]", "", (v or "").lower())
    if _s(a.get("place_name")) and (norm(_s(a["place_name"]).split(",")[0]) == norm(_s(a.get("address")).split(",")[0])
                                     or (not _s(a.get("address")) and re.match(r"^\d+\s", _s(a["place_name"])))):
        if not _s(a.get("address")):
            a = {**a, "address": a["place_name"]}
        a = {**a, "place_name": ""}
    for k, n in (("place_name", 140), ("address", 300), ("message", 2000)):
        if _s(a.get(k)):
            body[k] = _s(a[k])[:n]
    if body.get("message"):
        lines.append(f"Message: {body['message'][:200]}")
    # their homes to pick on the card (open houses / broker's opens), the host line for the preview
    from app.utils.plan_ids import listing_owner_key
    from app.utils.auth import luxe_theme_allowed
    homes = []
    owner_key = listing_owner_key(user)
    if owner_key:
        rows = (await db.execute(select(Property).where(
            or_(Property.list_agent_mls_id == owner_key, Property.co_list_agent_mls_id == owner_key),
            Property.home_status.in_(("ACTIVE-BRIGHT", "COMING SOON-BRIGHT", "ACTIVE UNDER CONTRACT-BRIGHT"))).order_by(Property.mls_list_date.desc().nulls_last()).limit(8))).scalars().all()
        for r in rows:
            photos = [x for x in (r.photos or []) if isinstance(x, str)][:6] if isinstance(r.photos, list) else []
            homes.append({"listing_key": r.listing_key, "address": tidy_caps(r.street_address) or "", "city": tidy_caps(r.city) or "",
                          "state": r.state, "zipcode": r.zipcode,
                          "price": r.price, "beds": r.bedrooms, "baths": r.bathrooms, "sqft": r.living_area,
                          "image": photos[0] if photos else None, "photos": photos})
    try:
        kit = await _run_route(user, "GET", "/studio/brand-kit") or {}
    except Exception:  # noqa: BLE001
        kit = {}
    try:
        hub = await _run_route(user, "GET", "/agent-hub/me") or {}
    except Exception:  # noqa: BLE001
        hub = {}
    prof = ((hub.get("config") or {}).get("profile") or {}) if isinstance(hub, dict) else {}
    host = {"hostName": prof.get("name") or " ".join(x for x in (user.first_name, user.last_name) if x) or None,
            "hostBrokerage": prof.get("brokerage") or user.brokerage, "hostPhoto": prof.get("photo_url") or user.photo_url,
            "hostArea": prof.get("market") or getattr(user, "market", None)}
    look = kit.get("look") or {}
    from app.services import invites_service
    options = {"homes": homes, "host": host, "luxe_ok": bool(luxe_theme_allowed(user)), "presets": {k: {x: v[x] for x in ("audience", "design", "message", "questions")} for k, v in invites_service.PRESETS.items()},
               "attached_cover": _s(a.get("cover_photo_url")) or None,
               "brand_accent": kit.get("secondary_color") or "#C9A24D", "brand_metal": look.get("metal") if look.get("theme") == "luxe" else None,
               "kind_given": bool(kind)}
    return Prepared("Make an event invite", lines + ["It gets its own link - nobody receives it until you send it from Forms & Invites"],
                    {"body": body, "options": options}, confirm_label="Make it", picker="invite")


async def _r_invite(db, user, p):
    body = dict(p["body"])
    home = body.get("linked_property") or {}
    if home.get("listing_key"):
        # A home's invite ties to its open house, so the guest page shows the Cheat Sheet (the builder does this)
        try:
            slots = await call_as_agent(user, "GET", "/invites/scheduled", params={"listing_key": home["listing_key"]}) or {}
            if slots.get("open_house_event_id"):
                body["open_house_event_id"] = slots["open_house_event_id"]
        except RouteError:
            pass
    out = await _run_route(user, "POST", "/invites", body)
    code = (out or {}).get("code")
    share = await _share(db, user, f"/i/{code}", url=(out or {}).get("share_url")) if code else None
    iid = (out or {}).get("id")
    return {"message": "Invite made - nobody has it yet. Say \"send it\" and I'll pick who it goes to, or share the link.",
            "link": f"/studio/forms?view=invites&invite={iid}" if iid else "/studio/forms?view=invites", "share": share}

def _adjust_invite(user, payload: dict, picks: dict) -> dict:
    """The card's taps -> the invite (the same fields the Forms & Invites builder saves)."""
    from app.utils.auth import luxe_theme_allowed
    body = dict(payload.get("body") or {})
    opts = payload.get("options") or {}
    if picks.get("kind") in INVITE_TYPES:
        body["invite_type"] = picks["kind"]
    home = next((h for h in (opts.get("homes") or []) if h.get("listing_key") == picks.get("listing_key")), None)
    if home:
        body["linked_property"] = {k: home.get(k) for k in ("listing_key", "address", "city", "state", "zipcode", "price", "beds", "baths", "sqft", "image", "photos")}
        body["address"] = ", ".join(x for x in (home.get("address"), home.get("city"), home.get("state")) if x)
        body.pop("place_name", None)
    elif "listing_key" in picks and not picks.get("listing_key"):
        body.pop("linked_property", None)
    if picks.get("date"):
        body["event_date"] = _date_ok(picks["date"])
    for k in ("start_time", "end_time"):
        if picks.get(k):
            body[k] = _time_ok(picks[k])
    if picks.get("design") in INVITE_DESIGNS:
        body["design"] = picks["design"]
    metal = picks.get("luxe_metal")
    if metal in ("gold", "silver", "bronze") and luxe_theme_allowed(user):
        body["luxe_metal"] = metal
        body.pop("accent_color", None)
    elif picks.get("accent_color") and _HEX.match(str(picks["accent_color"])):
        body["accent_color"] = str(picks["accent_color"])
        body["luxe_metal"] = None
    if _s(picks.get("title")):
        body["title"] = _s(picks["title"])[:140]
    # a home's street is the title (the card's line 2 shows the town) - also over a title that only
    # names the kind of event ("Broker's Open" under the card's own BROKER'S OPEN line, 11/01)
    generic = {re.sub(r"[^a-z]", "", v.lower()) for v in INVITE_TYPES.values()} | {"brokersopen", "openhouse", "invite", "youreinvited"}
    if home and (not _s(body.get("title")) or re.sub(r"[^a-z]", "", _s(body["title"]).lower()) in generic):
        body["title"] = home.get("address") or body.get("title")
    if not _s(body.get("title")):
        body["title"] = INVITE_TYPES.get(body.get("invite_type"), "You're invited")
    if not body.get("event_date"):
        raise ActionError("Pick the day on the card first.")
    # The rest of the builder's choices (owner, 11/05). A card always saves the kind's RSVP questions:
    # invites made from the chat used to have none, so nobody was asked how many were coming.
    preset = (opts.get("presets") or {}).get(body.get("invite_type")) or {}
    body["questions"] = picks["questions"][:12] if isinstance(picks.get("questions"), list) else preset.get("questions") or []
    body["message"] = _s(picks["message"])[:2000] if "message" in picks else body.get("message") or preset.get("message") or ""
    if body.get("invite_type") == "brokers_open":
        body["audience"] = "agents"
    else:
        body["audience"] = picks["audience"] if picks.get("audience") in ("clients", "agents") and body.get("invite_type") == "custom" else preset.get("audience") or "clients"
    if "subtitle" in picks:
        body["subtitle"] = None if picks["subtitle"] is None else _s(picks["subtitle"])[:60]
    if not home:
        for k, n in (("place_name", 140), ("address", 300)):
            if k in picks:
                if _s(picks.get(k)):
                    body[k] = _s(picks[k])[:n]
                else:
                    body.pop(k, None)
        if not _s(body.get("address")) and not _s(body.get("place_name")):
            raise ActionError("Add where it is on the card first.")
    cover = picks.get("cover")
    if isinstance(cover, str) and cover.startswith("http"):
        body["cover_image_url"] = cover
    elif cover is None and "cover" in picks and home:
        body["linked_property"] = {**body["linked_property"], "no_cover": True}
        body.pop("cover_image_url", None)
    elif home and home.get("image"):
        body["cover_image_url"] = home["image"]
    if picks.get("motion") in ("normal", "luxe"):
        body["motion"] = picks["motion"]
    days = []
    for d in (picks.get("extra_dates") or [])[:6]:
        if isinstance(d, dict) and d.get("date"):
            days.append({"date": _date_ok(d["date"]), "start": _time_ok(d.get("start") or body.get("start_time") or ""), "end": _time_ok(d.get("end") or body.get("end_time") or "")})
    if days:
        body["extra_dates"] = days
    return {**payload, "body": body}

register({"name": "create_invite", "description": (
    "Make an event invite with RSVP (open house, broker's open, client event, other). Set it up as soon as they ask - the card itself asks, as taps with a live preview, "
    "the kind, which of their listings, the days and times, the wording, the place, the cover photo, the design, color and animation, and the RSVP questions - so never ask those in chat; pass only what they already said "
    "(kind, title, date YYYY-MM-DD worked out from today when they say next Friday, times HH:MM, message, and cover_photo_url when they attached a photo). Keep the title short and catchy - under about 40 characters ('Holiday Cookie Swap', "
    "not the whole description; details go in message) - a long one is cut off on the invite card. place_name is a venue's NAME (a club, a restaurant) - never the street address; "
    "for a client event away from their listings ask where in one short question. Never sends it."),
    "input_schema": _obj({"kind": {"type": "string", "enum": list(INVITE_TYPES)}, "title": {"type": "string"}, "date": {"type": "string"}, "start_time": {"type": "string"}, "end_time": {"type": "string"},
                          "place_name": {"type": "string"}, "address": {"type": "string"}, "message": {"type": "string"}, "design": {"type": "string", "enum": list(INVITE_DESIGNS)},
                          "cover_photo_url": {"type": "string"}})},
    _p_invite, _r_invite, None, adjust=_adjust_invite)


BLOG_TONES = ("warm", "professional", "luxury", "energetic", "straight")
BLOG_THEMES = ("luxe", "luxe_plus", "editorial", "modern", "midnight")
BLOG_CATEGORIES = ("Buyers", "Sellers", "Market Updates", "Neighborhoods", "Homeownership", "Luxury", "Investments", "Open Houses", "Local Guides", "New Construction", "Real Estate News")
BLOG_LENGTHS = ("short", "standard", "long")
BLOG_AUDIENCES = ("Buyers", "Sellers", "First-time buyers", "Move-up buyers", "Luxury buyers", "Investors", "Relocating", "Homeowners")
BLOG_CTAS = ("how_can_i_help", "home_value", "showing", "buying", "selling", "make_me_sell", "question")


def _money_short(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    return f"${v / 1e6:.2f}".rstrip("0").rstrip(".") + "M" if v >= 1e6 else f"${round(v / 1e3)}K"


def _strs(raw, n: int, cap: int = 160) -> list:
    out = []
    for x in raw if isinstance(raw, list) else []:
        t = _s(x)[:cap]
        if t and t.lower() not in {o.lower() for o in out}:
            out.append(t)
    return out[:n]


def _yt(url: str) -> bool:
    import re
    return bool(re.search(r"(youtube\.com/(watch\?v=|shorts/|embed/|live/)|youtu\.be/)[\w-]{6,}", url or ""))


async def _p_blog(db, user, a):
    topic = _s(a.get("topic"))
    if not topic:
        raise ActionError("What should the blog post be about?")
    # The Blogs page's own interview as the card's steps (owner, 11/01: "exactly how it would be if you
    # were actually on the blog ... questions answered by checking off boxes"): Topic (an angle + what
    # readers walk away with), Readers (who + towns + Town Spotlight), Key points (ideas to check off,
    # their take, numbers), Your homes (sales + listings), Video (their YouTube, "Fits" first), Style
    # (tone, length with its share of the AI allowance, the ending button).
    percents = {}
    for ln in BLOG_LENGTHS:
        try:
            est = await call_as_agent(user, "GET", "/studio/blogs/ai/estimate", params={"length": ln})
        except RouteError:
            est = None
        if (est or {}).get("percent") is not None:
            percents[ln] = est["percent"]
    try:
        from app.services.ai import ai_blog_writer_service
        ctx = await ai_blog_writer_service.build_context(db, user)
    except Exception:  # noqa: BLE001
        logger.warning("blog context for the chat card failed", exc_info=True)
        ctx = {}
    areas = [x for x in (ctx.get("served_areas") or []) if x][:10]
    cards = {str(c.get("name") or "").lower(): c.get("slug") for c in (ctx.get("area_cards") or []) if c.get("slug")}
    # every sale (newest first) - the card recommends the ones in the towns picked and shows 5 at a time
    sales = [{"id": str(x.get("id")), "address": x.get("address") or "", "city": x.get("city") or "", "price": x.get("price"),
              "date": str(x.get("sold_date") or "")[:7], "photo": x.get("image_url")}
             for x in (ctx.get("recent_sales") or [])[:60] if x.get("id")]
    listings = [{k: x.get(k) for k in ("id", "address", "city", "state", "price", "beds", "baths", "sqft", "status", "image_url")}
                for x in (ctx.get("listings") or [])[:12] if x.get("id")]
    videos = [{k: v.get(k) for k in ("id", "title", "url", "image", "kind")} for v in (ctx.get("videos") or []) if _yt(v.get("url") or "")][:24]
    want_areas = [t for t in (a.get("areas") or []) if isinstance(t, str) and t.strip()][:5]
    body = {"topic": topic[:500], "tone": a.get("tone") if a.get("tone") in BLOG_TONES else "warm",
            "length": a.get("length") if a.get("length") in BLOG_LENGTHS else "standard",
            "audiences": [x for x in (a.get("audiences") or []) if x in BLOG_AUDIENCES],
            "areas": want_areas, "cta": a.get("cta") if a.get("cta") in BLOG_CTAS else "how_can_i_help",
            "area_slugs": [cards[t.lower()] for t in want_areas if t.lower() in cards]}
    if _s(a.get("key_points")):
        body["key_points"] = _s(a["key_points"])[:2000]
    if _s(a.get("goal")):
        body["goal"] = _s(a["goal"])[:300]
    from app.utils.auth import luxe_blog_theme_allowed
    options = {"areas": areas, "area_cards": cards, "sales": sales, "listings": listings, "videos": videos, "luxe_ok": bool(luxe_blog_theme_allowed(user)),
               "idx_ok": user.plan_tier == "IDX", "attached_cover": _s(a.get("cover_photo_url")) or None,
               "angles": _strs(a.get("angles"), 5, 200), "goals": _strs(a.get("goals"), 4, 200), "point_ideas": _strs(a.get("point_ideas"), 7, 200)}
    return Prepared("Write a blog post with AI", [f"Topic: {topic[:200]}", "Saved as a draft - never published until you do"],
                    {"body": body, "percents": percents, "options": options},
                    warning="This uses your monthly AI allowance.", confirm_label="Write it", picker="blog")


def _adjust_blog(user, payload: dict, picks: dict) -> dict:
    out = dict(payload)
    body = dict(out.get("body") or {})
    opts = out.get("options") or {}
    if _s(picks.get("topic")):
        body["topic"] = _s(picks["topic"])[:500]
    if picks.get("tone") in BLOG_TONES:
        body["tone"] = picks["tone"]
    if picks.get("length") in BLOG_LENGTHS:
        body["length"] = picks["length"]
    if picks.get("cta") in BLOG_CTAS:
        body["cta"] = picks["cta"]
    if isinstance(picks.get("audiences"), list):
        body["audiences"] = [x for x in picks["audiences"] if x in BLOG_AUDIENCES]
    if isinstance(picks.get("areas"), list):
        body["areas"] = [str(t).strip() for t in picks["areas"] if str(t).strip()][:5]
        cards = opts.get("area_cards") or {}
        body["area_slugs"] = [cards[t.lower()] for t in body["areas"] if t.lower() in cards] if picks.get("spotlight", True) else []
    if isinstance(picks.get("sale_ids"), list):
        allowed = {x["id"] for x in (opts.get("sales") or [])}
        body["sale_ids"] = [str(i) for i in picks["sale_ids"] if str(i) in allowed][:4]
        body["include_recent_sales"] = bool(body["sale_ids"])
    if isinstance(picks.get("listing_ids"), list):
        by = {x["id"]: x for x in (opts.get("listings") or [])}
        body["listings"] = [{"listing_key": by[i]["id"], **{k: by[i][k] for k in ("address", "city", "state", "price", "beds", "baths", "sqft", "status", "image_url") if by[i].get(k) is not None}}
                            for i in picks["listing_ids"] if i in by][:6]
    # The article's look and where it shows (owner, 11/05): set on the draft as soon as it exists.
    after, places = {}, {}
    if picks.get("theme") in BLOG_THEMES and (picks["theme"] != "luxe_plus" or opts.get("luxe_ok")):
        after["theme"] = picks["theme"]
        if picks["theme"] == "luxe_plus" and picks.get("luxe_metal") in ("gold", "silver", "bronze"):
            after["luxe_metal"] = picks["luxe_metal"]
        if picks["theme"] == "luxe" and picks.get("accent_color") and _HEX.match(str(picks["accent_color"])):
            after["accent_color"] = str(picks["accent_color"])
    if picks.get("category") in BLOG_CATEGORIES:
        after["category"] = picks["category"]
        body["category_hint"] = picks["category"]
    if isinstance(picks.get("cover"), str) and picks["cover"].startswith("http"):
        after["cover_image_url"] = picks["cover"]
    for k in ("agent_hub_visible", "agent_hub_featured", "use_signature_card") + (("idx_visible",) if opts.get("idx_ok") else ()):
        if isinstance(picks.get(k), bool):
            places[k] = picks[k]
    out["after"], out["places"] = after, places
    if "video" in picks:
        v = picks.get("video") if isinstance(picks.get("video"), dict) else None
        if v and _yt(_s(v.get("url"))):
            body["video"] = {"url": _s(v["url"])[:400], **({"title": _s(v.get("title"))[:200]} if _s(v.get("title")) else {})}
        else:
            body.pop("video", None)
    # checked key points + their own, as bullets (the writer reads them like the page's box)
    if isinstance(picks.get("points"), list):
        pts = _strs(picks["points"], 12, 300)
        own = _s(picks.get("key_points"))
        text = "\n".join(f"- {x}" for x in pts) + (("\n" if pts else "") + own if own else "")
        if text.strip():
            body["key_points"] = text[:2000]
        else:
            body.pop("key_points", None)
    for k, cap in (("personal_take", 2000), ("facts", 2000), ("goal", 300)):
        if k in picks:
            v = _s(picks.get(k))[:cap]
            if v:
                body[k] = v
            else:
                body.pop(k, None)
    out["body"] = body
    return out


async def _r_blog(db, user, p):
    out = await _run_route(user, "POST", "/studio/blogs/ai/generate-job", p["body"])
    bid = (out or {}).get("blog_id") or (out or {}).get("id")
    if not bid:
        return {"message": "Writing it now - it takes a few minutes.", "link": "/studio/blogs"}
    if p.get("after"):
        await _run_route(user, "PUT", f"/studio/blogs/{bid}", p["after"])
    if p.get("places"):
        await _run_route(user, "PATCH", f"/studio/blogs/{bid}/agent-hub-visibility", p["places"])
    # It writes in the background (research, writing, fact-check): the card keeps working and the
    # chat says when the draft is ready - like an AI CMA (owner, 11/01).
    from app.services.ai import ai_notify
    from app.services.ai.ai_actions import CARD
    await ai_notify.watch(db, user, f"blog:{bid}", CARD.get() or {}, _s((p.get("body") or {}).get("topic"))[:80] or "your topic")
    return {"working": True, "link": f"/studio/blogs/{bid}",
            "message": "Writing your draft now - researching, writing and fact-checking takes a few minutes. I'll tell you right here when it's "
                       "ready, even if you close the site."}

_simple("write_blog_post", "Start the AI blog writer on a topic (draft only; uses the agent's AI allowance). Plans with Blogs. "
        "You CAN write blog posts - never say otherwise. The card is the Blogs page's own interview as check-box steps (topic angle + what readers walk away with, who it's for, towns + Town Spotlight, key points, their take and numbers, their sales and listings, one of their YouTube videos, tone, length, the ending button, the article's look and color, its category and cover photo, and where it shows) - "
        "set it up as soon as you have the topic and never ask those in chat (pass them only if they already said). ALWAYS pass angles (4-5 sharper titles on their topic), goals (3-4 one-line things a reader should walk away knowing or doing) and point_ideas (5-7 specific key points worth covering) - they're the check boxes on the card.",
        _obj({"topic": {"type": "string"}, "key_points": {"type": "string"}, "goal": {"type": "string"}, "tone": {"type": "string", "enum": list(BLOG_TONES)},
              "length": {"type": "string", "enum": list(BLOG_LENGTHS)}, "audiences": {"type": "array", "items": {"type": "string", "enum": list(BLOG_AUDIENCES)}},
              "areas": {"type": "array", "items": {"type": "string"}, "description": "Towns the article is about, if they named any (up to 5)."},
              "cta": {"type": "string", "enum": list(BLOG_CTAS)},
              "angles": {"type": "array", "items": {"type": "string"}}, "goals": {"type": "array", "items": {"type": "string"}},
              "point_ideas": {"type": "array", "items": {"type": "string"}}, "cover_photo_url": {"type": "string", "description": "A photo they attached, for the cover."}}, ["topic"]), _p_blog, _r_blog, STUDIO, adjust=_adjust_blog)


# ================================================================ SETTINGS / PROFILE / SITE

async def _p_profile(db, user, a):
    profile = {k: _s(a[k]) for k in ("name", "title", "brokerage", "team", "market", "about") if _s(a.get(k))}
    if _s(a.get("brokerage_phone")):
        profile["brokeragePhone"] = _s(a["brokerage_phone"])
    contact = {k: _s(a[src]) for k, src in (("call", "phone_call"), ("text", "phone_text"), ("email", "contact_email")) if _s(a.get(src))}
    if "about" in profile and len(profile["about"]) > 280:
        raise ActionError("Your bio needs to fit in 280 characters.")
    if not profile and not contact:
        raise ActionError("What should change in your profile?")
    labels = {"brokeragePhone": "Brokerage phone", "name": "Name", "title": "Title", "brokerage": "Brokerage", "team": "Team", "market": "Market", "about": "Bio", "call": "Call number", "text": "Text number", "email": "Contact email"}
    lines = [f"{labels[k]}: {v}" for k, v in {**profile, **contact}.items()]
    body = {**({"profile": profile} if profile else {}), **({"contact": contact} if contact else {})}
    return Prepared("Update your profile", lines + ["Shows on your Agent Hub, forms and emails"], {"body": body},
                    warning="If you have Settings or the Agent Hub editor open in another tab, refresh it first so it doesn't save over this.")


async def _r_profile(db, user, p):
    await _run_route(user, "PATCH", "/setup/info", p["body"])
    return {"message": "Profile saved.", "link": "/settings"}

_simple("update_profile", "Change the agent's profile: name, title, brokerage, brokerage phone (print ads need it), team, market, bio (<=280 chars), call/text numbers (phone_call is their mobile on designs), contact email. A headshot goes through update_branding (headshot_url, or the card's Headshot step).",
        _obj({"name": {"type": "string"}, "title": {"type": "string"}, "brokerage": {"type": "string"}, "team": {"type": "string"}, "market": {"type": "string"},
              "about": {"type": "string"}, "brokerage_phone": {"type": "string"}, "phone_call": {"type": "string"}, "phone_text": {"type": "string"}, "contact_email": {"type": "string"}}),
        _p_profile, _r_profile)

EMAIL_CATS = {"leads": "New leads & sign-ins", "showcase_requests": "Client requests in Showcases", "showcase_activity": "Showcase activity",
              "showcase_digest": "Daily briefing", "spotlight_weekly": "Spotlight weekly report", "liked_homes": "Liked homes", "host_requests": "Open house host requests",
              "team": "Team & broadcasts", "listing_updates": "My listing updates", "ai_media": "AI media ready", "reviews": "New reviews", "account": "Account & support"}


async def _p_email_settings(db, user, a):
    body, lines = {}, []
    if _s(a.get("briefing_time")):
        t = _time_ok(a["briefing_time"])
        h, m = int(t[:2]), int(t[3:])
        if m % 15 or not (6 <= h <= 20 or t == "21:00"):
            raise ActionError("The briefing can arrive any quarter hour from 6:00 AM to 9:00 PM Eastern.")
        body["briefing_time"] = t; lines.append(f"Daily briefing arrives at {_nice_time(t)} Eastern")
    if a.get("showcase_invite_delay_minutes") in (0, 15, 30, 60):
        body["invite_delay"] = a["showcase_invite_delay_minutes"]; lines.append(f"Showcase invite after an open house: {a['showcase_invite_delay_minutes'] or 'right away'}{' min later' if a['showcase_invite_delay_minutes'] else ''}")
    rec = {}
    for cat, on in (a.get("emails_to_me") or {}).items():
        if cat in EMAIL_CATS and isinstance(on, bool):
            rec[cat] = on; lines.append(f"Email me about {EMAIL_CATS[cat]}: {_yn(on)}")
    if rec:
        body["receive"] = rec
    if not body:
        raise ActionError("Which email setting should change?")
    return Prepared("Email settings", lines, {"body": body})


async def _r_email_settings(db, user, p):
    await _run_route(user, "PUT", "/api/v1/email-settings", p["body"])
    return {"message": "Saved.", "link": "/studio/email"}

_simple("update_email_settings", "Change the agent's email settings: daily briefing time (HH:MM, quarter hours 06:00-21:00 Eastern), Showcase invite delay after an open house (0/15/30/60 min), and which emails they get (emails_to_me: {category: true/false}; categories: " + ", ".join(EMAIL_CATS) + ").",
        _obj({"briefing_time": {"type": "string"}, "showcase_invite_delay_minutes": {"type": "integer", "enum": [0, 15, 30, 60]},
              "emails_to_me": {"type": "object", "additionalProperties": {"type": "boolean"}}}), _p_email_settings, _r_email_settings)


async def _p_site_on(db, user, a):
    on = bool(a.get("on"))
    return Prepared(f"Turn your IDX website {'on' if on else 'off'}", ["Comes back exactly as you published it" if on else "Your site stops loading; new links and QR codes use openhousepal.com - nothing is deleted"],
                    {"on": on}, warning=None if on else "Anyone visiting your website will see it's unavailable.", confirm_label="Turn it on" if on else "Turn it off")


async def _r_site_on(db, user, p):
    await _run_route(user, "PUT", "/studio/sites/me/site-enabled", {"enabled": p["on"]})
    return {"message": f"Website turned {'on' if p['on'] else 'off'}.", "link": "/studio/website"}

_simple("set_website_on", "Turn the agent's IDX website on or off (content is kept). IDX plan.", _obj({"on": {"type": "boolean"}}, ["on"]), _p_site_on, _r_site_on, IDX)


async def _p_town(db, user, a):
    name, state = _s(a.get("town")), (_s(a.get("state")) or "PA").upper()[:2]
    if not name:
        raise ActionError("Which town?")
    kind = "neighborhood" if a.get("neighborhood") else "city"
    return Prepared(f"Add {name}, {state} to your towns", ["Used for your forms, buyer matching and blog town cards" + (" - and a town page on your website" if user.plan_tier == "IDX" else ""), "Up to 15 towns"],
                    {"body": {"name": name, "state": state, "area_type": kind}})


async def _r_town(db, user, p):
    await _run_route(user, "POST", "/studio/sites/me/areas", p["body"])
    return {"message": "Town added - its info fills in over the next few minutes.", "link": "/studio/website"}

_simple("add_town", "Add a town/area the agent serves (state 2 letters, default PA; neighborhood=true for a neighborhood).",
        _obj({"town": {"type": "string"}, "state": {"type": "string"}, "neighborhood": {"type": "boolean"}}, ["town"]), _p_town, _r_town)


async def _p_review_settings(db, user, a):
    body, lines = {}, []
    if isinstance(a.get("show_on_hub"), bool):
        body["show_on_hub"] = a["show_on_hub"]; lines.append(f"Reviews on my Agent Hub: {_yn(a['show_on_hub'])}")
    if isinstance(a.get("show_on_website"), bool):
        body["show_on_idx"] = a["show_on_website"]; lines.append(f"Reviews on my website: {_yn(a['show_on_website'])}")
    if isinstance(a.get("only_4_and_5_stars"), bool):
        body["min_rating"] = 4 if a["only_4_and_5_stars"] else 1; lines.append("Only show 4 and 5 stars" if a["only_4_and_5_stars"] else "Show every rating")
    for k, label in (("google_review_link", "google_review_url"), ("zillow_review_link", "zillow_review_url")):
        if _s(a.get(k)):
            if not _s(a[k]).startswith("http"):
                raise ActionError("Review links need to start with https://")
            body[label] = _s(a[k]); lines.append(f"{'Google' if 'google' in k else 'Zillow'} review link: {body[label]}")
    for rid, show in (a.get("show_reviews") or {}).items():
        if _ID.match(str(rid)) and isinstance(show, bool):
            r = await _one(db, select(AgentReview).where(AgentReview.id == rid, AgentReview.owner_id == user.id), "I couldn't find that review.")
            body.setdefault("_reviews", {})[rid] = show; lines.append(f"{'Show' if show else 'Hide'} {r.reviewer_name}'s review")
    if not body:
        raise ActionError("Which review setting should change?")
    return Prepared("Review settings", lines, {"body": body})


async def _r_review_settings(db, user, p):
    body = dict(p["body"]); one = body.pop("_reviews", {})
    if body:
        await _run_route(user, "PATCH", "/agent-hub/reviews/settings", body)
    for rid, show in one.items():
        await _run_route(user, "PATCH", f"/agent-hub/reviews/{rid}", {"status": "published" if show else "hidden"})
    return {"message": "Saved.", "link": "/studio/reviews"}

_simple("update_review_settings", "Reviews: show on Agent Hub / website, only 4-5 stars, Google/Zillow review links, and show/hide single reviews (show_reviews: {review_id: true/false} from get_my_reviews).",
        _obj({"show_on_hub": {"type": "boolean"}, "show_on_website": {"type": "boolean"}, "only_4_and_5_stars": {"type": "boolean"},
              "google_review_link": {"type": "string"}, "zillow_review_link": {"type": "string"}, "show_reviews": {"type": "object", "additionalProperties": {"type": "boolean"}}}),
        _p_review_settings, _r_review_settings, REVIEWS)


async def _p_ticket(db, user, a):
    kind = a.get("kind") if a.get("kind") in ("support", "bug", "feature") else "support"
    subject, msg = _s(a.get("subject")), _s(a.get("message"))
    if not subject or not msg:
        raise ActionError("What should the ticket say?")
    return Prepared(f"Send the OpenHousePal team a {'bug report' if kind == 'bug' else 'feature idea' if kind == 'feature' else 'question'}",
                    [f"Subject: {subject[:200]}", msg[:600]], {"body": {"subject": subject[:200], "message": msg, "category": kind}},
                    warning="This goes to a real person on the OpenHousePal team.", confirm_label="Send it")


async def _r_ticket(db, user, p):
    await _run_route(user, "POST", "/api/v1/support/tickets", p["body"])
    return {"message": "Sent - you'll see their reply under Help.", "link": "/settings/help"}

_simple("open_help_ticket", "Send the OpenHousePal team a Help ticket (kind: support question, bug, or feature idea). For bugs write 'What happened:', 'What I expected:', 'Where:'.",
        _obj({"kind": {"type": "string", "enum": ["support", "bug", "feature"]}, "subject": {"type": "string"}, "message": {"type": "string"}}, ["subject", "message"]),
        _p_ticket, _r_ticket)


# ================================================================ STUDIO DESIGNS (built in the browser)
# Mirrors frontend lib/studio/designSituations.ts + designFormats.ts + designTemplates.ts
# READY_COMPOSITIONS - keep in step when a situation, format or layout is added or retired.

DESIGN_SITUATIONS = {
    "just_listed": ("Just Listed", "listings", ["just_listed_flyer", "postcard", "postcard_jumbo", "instagram_post", "instagram_portrait", "instagram_story"]),
    "coming_soon": ("Coming Soon", "listings", ["postcard", "postcard_jumbo", "letter_mailer", "instagram_post", "instagram_portrait", "instagram_story"]),
    "open_house": ("Open House", "open_house", ["open_house_flyer", "postcard", "postcard_jumbo", "door_hanger", "instagram_post", "instagram_portrait", "instagram_story"]),
    "price_improvement": ("Price Improvement", "listings", ["postcard", "postcard_jumbo", "letter_mailer", "instagram_post", "instagram_portrait", "instagram_story"]),
    "under_contract": ("Under Contract", "listings", ["postcard", "postcard_jumbo", "instagram_post", "instagram_portrait", "instagram_story"]),
    "just_sold": ("Just Sold", "sales", ["postcard", "postcard_jumbo", "letter_mailer", "door_hanger", "instagram_post", "instagram_portrait", "instagram_story"]),
    "home_valuation": ("Home Valuation", None, ["postcard", "postcard_jumbo", "letter_mailer", "door_hanger", "instagram_post", "instagram_portrait"]),
    "make_me_sell": ("Make Me Sell", None, ["postcard", "postcard_jumbo", "letter_mailer", "door_hanger", "instagram_post", "instagram_portrait"]),
    "find_your_dream_home": ("Find Your Dream Home", None, ["postcard", "postcard_jumbo", "instagram_post", "instagram_portrait", "instagram_story"]),
    "social_ad": ("Social Media Ad", None, ["instagram_portrait", "instagram_post", "instagram_story"]),
}
DESIGN_FORMATS = {"just_listed_flyer": "Just Listed Flyer", "open_house_flyer": "Open House Flyer", "postcard": "Postcard", "postcard_jumbo": "Jumbo Postcard",
                  "letter_mailer": "Letter Mailer", "door_hanger": "Door Hanger", "instagram_post": "Square Post", "instagram_portrait": "Feed Post",
                  "instagram_story": "Instagram Story"}
DESIGN_LAYOUTS = {"keepsake": "Keepsake", "billboard": "Billboard", "slant": "Slant", "postage": "Postage", "filmstrip": "Filmstrip", "luxe": "Luxe"}
DESIGN_COLORS = {"light": "Ivory", "stone": "Stone", "dark": "Charcoal", "forest": "Forest", "brand": "My brand colors", "luxe": "Luxe"}


async def _p_design(db, user, a):
    sit = _s(a.get("situation"))
    if sit not in DESIGN_SITUATIONS:
        raise ActionError("Which kind of design - Just Listed, Open House, Just Sold, Home Valuation...?")
    label, rule, formats = DESIGN_SITUATIONS[sit]
    fmt = _s(a.get("format"))
    if fmt not in formats:
        raise ActionError(f"A {label} design comes as: " + ", ".join(DESIGN_FORMATS[f] for f in formats) + ".")
    layout = _s(a.get("layout")) or None
    color = _s(a.get("colors")) or "light"
    idx = user.plan_tier == "IDX" or user.is_admin
    if layout and layout not in DESIGN_LAYOUTS:
        raise ActionError("Layouts are Keepsake, Billboard, Slant, Postage, Filmstrip and Luxe.")
    if (layout == "luxe" or color == "luxe") and not idx:
        raise ActionError("The Luxe look comes with the IDX plan.")
    if color not in DESIGN_COLORS:
        color = "light"
    # "Open House" + "Open House Flyer" reads "Open House Flyer", not "Open House Open House Flyer" (owner's test, 11/01)
    kind = DESIGN_FORMATS[fmt] if DESIGN_FORMATS[fmt].lower().startswith(label.lower()) else f"{label} {DESIGN_FORMATS[fmt]}"
    cname = DESIGN_COLORS[color]
    lines = [kind, f"Look: {DESIGN_LAYOUTS.get(layout, 'the recommended layout')}, {cname if cname.endswith('colors') else cname + ' colors'}"]
    payload = {"situation": sit, "format": fmt, "layout": layout, "colors": color}
    if rule:
        key = _need_id(a, "listing_key")
        prop = await _one(db, select(Property).where(Property.listing_key == key), "I couldn't find that home in the listings.")
        mine = bool(user.mls_id) and user.mls_id in (prop.list_agent_mls_id, prop.co_list_agent_mls_id)
        status = (prop.home_status or "").lower()
        if rule == "listings" and not mine:
            raise ActionError("Only your own listings can be advertised this way - that's the MLS and REALTOR® rule.")
        if rule == "sales" and not ("closed" in status or "sold" in status):
            raise ActionError("That home hasn't sold, so it can't be a Just Sold design yet.")
        if rule == "sales" and not mine:
            raise ActionError("Only sales you represented can be called Sold. If you were the buyer's agent, make it from the Studio page and confirm that there.")
        if rule == "open_house" and not mine:
            hosting = (await db.execute(select(OpenHouseHostRequest.id).where(
                OpenHouseHostRequest.listing_key == key, OpenHouseHostRequest.requesting_agent_id == user.id,
                OpenHouseHostRequest.status == "approved"))).first()
            if not hosting:
                raise ActionError("An Open House design has to be your own listing, or one its listing agent approved you to host.")
        addr = tidy_caps(", ".join(x for x in (prop.street_address, prop.city) if x))   # the MLS sends ALL CAPS
        payload.update(listing_key=key, address=addr)
        lines.insert(1, f"Home: {addr}")
        # Their other homes of the same kind, a tap away on the card (owner, 11/01: "no, a different
        # listing of mine" twice and the card kept the newest one): own active listings, or own sales.
        from app.utils.plan_ids import listing_owner_key
        owner_key = listing_owner_key(user)
        if owner_key and rule in ("listings", "open_house", "sales"):
            statuses = ("CLOSED-BRIGHT",) if rule == "sales" else ("ACTIVE-BRIGHT", "COMING SOON-BRIGHT")
            rows = (await db.execute(select(Property).where(
                or_(Property.list_agent_mls_id == owner_key, Property.co_list_agent_mls_id == owner_key),
                Property.home_status.in_(statuses)).order_by(Property.mls_list_date.desc().nulls_last()).limit(8))).scalars().all()
            homes = [{"listing_key": r.listing_key, "address": tidy_caps(", ".join(x for x in (r.street_address, r.city) if x)) or "",
                      "photo": (r.photos[0] if isinstance(r.photos, list) and r.photos and isinstance(r.photos[0], str) else None)} for r in rows]
            if not any(h["listing_key"] == key for h in homes):
                homes.insert(0, {"listing_key": key, "address": addr, "photo": (prop.photos[0] if isinstance(prop.photos, list) and prop.photos and isinstance(prop.photos[0], str) else None)})
            if len(homes) > 1:
                payload["homes"] = homes
        if sit == "open_house" and _s(a.get("open_house_date")):
            d = _date_ok(a["open_house_date"]); payload["open_house_date"] = d
            s_, e_ = _s(a.get("start_time")), _s(a.get("end_time"))
            if s_:
                payload["start_time"] = _time_ok(s_)
            if e_:
                payload["end_time"] = _time_ok(e_)
            lines.append(f"Open house: {_nice_date(d)}" + (f", {_nice_time(payload['start_time'])}" if payload.get("start_time") else "") + (f"-{_nice_time(payload['end_time'])}" if payload.get("end_time") else ""))
    elif _s(a.get("headline")):
        # whole words only - a cut at 90 characters printed "...no-pressure estim" on the design (11/02)
        h = _s(a["headline"])
        payload["headline"] = h if len(h) <= 90 else h[:90].rsplit(" ", 1)[0].rstrip(",.;:-")
        lines.append(f"Headline: “{payload['headline']}”")
    lines.append("Made in your Studio - you'll see it here with a Download button, and can still edit it")
    return Prepared(f"Make {'an' if kind[:1].lower() in 'aeiou' else 'a'} {kind}", lines, payload, confirm_label="Make it")

register({"name": "create_design", "description": (
    "Make a Studio design (flyer, postcard, door hanger, social post...) that appears in the chat with a Download button. "
    "situation: " + ", ".join(DESIGN_SITUATIONS) + ". format must be one the situation offers: "
    + "; ".join(f"{k}: {', '.join(v[2])}" for k, v in DESIGN_SITUATIONS.items())
    + ". just_listed/coming_soon/price_improvement/under_contract need one of the agent's own listings (listing_key); just_sold a sale they had; open_house their listing or one they're approved to host (+ optional date/time). "
    "The others (home_valuation, make_me_sell, find_your_dream_home, social_ad) need no home - optional headline (short and punchy, under about 60 characters - it's printed big). layout: keepsake, billboard, slant, postage, filmstrip, luxe (IDX). colors: light (Ivory), stone, dark (Charcoal), forest, brand, luxe (IDX). "
    "The card itself lets the agent pick the layout and colors before tapping Make it - don't ask about the look first; pass layout/colors only when they named one. "
    "The card also lists their other listings (or sales) to switch to. When they say it's a different / another listing, never offer the same home again: use the one they name, or list their listings and ask which. "
    "Use the listing_key from their listings/sales list in your instructions - never a search result for a look-alike address."),
    "input_schema": _obj({"situation": {"type": "string", "enum": list(DESIGN_SITUATIONS)}, "format": {"type": "string", "enum": list(DESIGN_FORMATS)},
                          "listing_key": {"type": "string"}, "layout": {"type": "string", "enum": list(DESIGN_LAYOUTS)}, "colors": {"type": "string", "enum": list(DESIGN_COLORS)},
                          "headline": {"type": "string"}, "open_house_date": {"type": "string"}, "start_time": {"type": "string"}, "end_time": {"type": "string"}},
                         ["situation", "format"])},
         _p_design, None, STUDIO, client="create_design")



# ================================================================ OPEN HOUSE KIT
# Owner, 11/01 (capability review: "it can't build an Open House Kit, the core feature"): the card picks
# the home, the cover photo, the kit's pieces and what happens after a guest signs in; "Build my kit"
# runs the Opens page's own kit maker with those choices (the browser does it, like a design - the same
# QR sign-in, Similar Homes, Cheat Sheet and saves) and stops on Review & Save for the agent's tap.

async def _p_kit(db, user, a):
    from app.utils.plan_ids import listing_owner_key
    mine = listing_owner_key(user)
    key = _s(a.get("listing_key"))
    rows = []
    if mine:
        rows = list((await db.execute(select(Property).where(
            or_(Property.list_agent_mls_id == mine, Property.co_list_agent_mls_id == mine),
            Property.home_status.in_(("ACTIVE-BRIGHT", "COMING SOON-BRIGHT"))).order_by(Property.mls_list_date.desc().nulls_last()).limit(8))).scalars().all())
    if key and not any(r.listing_key == key for r in rows):
        other = (await db.execute(select(Property).where(Property.listing_key == key))).scalars().first()
        if other is None:
            raise ActionError("I couldn't find that home in the listings.")
        rows.insert(0, other)
    if not rows:
        raise ActionError("Which home is the open house at? Give me the address and I'll set it up.")

    def home(r):
        photos = [x for x in (r.photos if isinstance(r.photos, list) else []) if isinstance(x, str) and not x.startswith("data:")][:12]
        return {"listing_key": r.listing_key, "address": r.full_address or r.street_address or "", "street": tidy_caps(r.street_address) or "",
                "city": tidy_caps(r.city) or "", "lat": r.latitude, "lng": r.longitude, "price": r.price, "photos": photos,
                "own": bool(mine) and mine in (r.list_agent_mls_id, r.co_list_agent_mls_id)}
    homes = [home(r) for r in rows]
    pick = next((h for h in homes if h["listing_key"] == key), homes[0])
    return Prepared("Build an Open House Kit", ["QR sign-in sheet, flyer and the pieces you pick", "Built on your Opens page - you'll look it over and tap Save"],
                    {"homes": homes, "listing_key": pick["listing_key"]}, confirm_label="Build my kit", picker="kit")

register({"name": "build_open_house_kit", "description": (
    "Build an Open House Kit (QR sign-in sheet, flyer, Brag Sheet, Similar Homes, Cheat Sheet) for one of the agent's listings or a home they're hosting. "
    "The card asks which home (their active listings; pass listing_key for another home they named, from search_properties), the cover photo, "
    "the kit's pieces and what happens after a guest signs in - set it up right away and never ask those in chat. Building it opens their Opens page "
    "with everything done; they look it over and tap Save."),
    "input_schema": _obj({"listing_key": {"type": "string"}})},
    _p_kit, None, MLS, client="build_open_house_kit")



# ================================================================ DISCOVERY SEARCH AREA
# Owner, 11/01: "if they have not set a filter yet in their discovery, the chat bot should say that ...
# what are you looking for, and basically make that filter for them in discovery, and it saves". The
# card's center choices are their listings, their towns and the place they named - placed from our own
# MLS copy (a town = the middle of its homes), never a paid geocode - plus miles, a price floor and beds.
# It saves through the Discovery page's own PATCH, so the page shows the same filter.

async def _town_center(db, town: str):
    t = (town or "").split(",")[0].strip()
    if not t:
        return None
    # One state only: the same town name in two states must never land between them.
    want = re.search(r",\s*([A-Za-z]{2})\b", town or "")
    q = (select(func.avg(Property.latitude), func.avg(Property.longitude), func.count(Property.id), Property.state)
         .where(func.lower(Property.city) == t.lower(), Property.latitude.isnot(None)).group_by(Property.state).order_by(func.count(Property.id).desc()))
    if want:
        q = q.where(func.upper(Property.state) == want.group(1).upper())
    row = (await db.execute(q.limit(1))).first()
    if not row or not row[2]:
        return None
    return {"label": f"{tidy_caps(t)}, {row[3] or ''}".strip(", "), "lat": round(float(row[0]), 6), "lng": round(float(row[1]), 6), "kind": "town"}


async def _address_center(db, text: str, mine: Optional[str] = None):
    # An address starts with its house number ("417 Dogwood" is never "42417 Dogwood Glen Sq") - and
    # the agent's own listing wins over a look-alike elsewhere.
    t = (text or "").strip()
    street = t.split(",")[0].strip()
    if not re.match(r"^\d+\s+\S", street):
        return None
    q = select(Property).where(Property.street_address.ilike(f"{street}%"), Property.latitude.isnot(None))
    town = t.split(",")[1].strip() if "," in t else ""
    if town and not re.match(r"^[A-Za-z]{2}(\s+\d{5})?$", town):   # "12 Oak St, Wayne": this Oak St, not the first one anywhere
        q = q.where(func.lower(Property.city) == town.lower())
    if mine:
        q = q.order_by((or_(Property.list_agent_mls_id == mine, Property.co_list_agent_mls_id == mine)).desc())
    p = (await db.execute(q.limit(1))).scalars().first()
    if p is None:
        return None
    return {"label": ", ".join(x for x in (tidy_caps(p.street_address), tidy_caps(p.city)) if x), "lat": p.latitude, "lng": p.longitude, "kind": "home"}


async def _p_discovery(db, user, a):
    from app.utils.plan_ids import listing_owner_key
    try:
        cur = (await call_as_agent(user, "GET", "/api/discovery")) or {}
    except RouteError:
        cur = {}
    prefs = cur.get("preferences") or {}
    centers = []
    mine = listing_owner_key(user)
    want = _s(a.get("center"))
    unresolved = None
    if want:
        c = await _address_center(db, want, mine) or await _town_center(db, want)
        if c:
            centers.append(c)
        else:
            unresolved = want   # a home address, a landmark: the card's address box finds it (Google, like the page)
    # Their center as it is now stays the pick unless they named a new one (owner, 11/01: "keep a
    # centrepoint ... in the same radius").
    if prefs.get("landmark_address") and isinstance(prefs.get("latitude"), (int, float)) and isinstance(prefs.get("longitude"), (int, float)):
        centers.insert(len(centers), {"label": prefs["landmark_address"], "lat": prefs["latitude"], "lng": prefs["longitude"], "kind": "current"})
    if mine:
        for r in (await db.execute(select(Property).where(
                or_(Property.list_agent_mls_id == mine, Property.co_list_agent_mls_id == mine),
                Property.home_status.in_(("ACTIVE-BRIGHT", "COMING SOON-BRIGHT")), Property.latitude.isnot(None))
                .order_by(Property.mls_list_date.desc().nulls_last()).limit(4))).scalars().all():
            centers.append({"label": ", ".join(x for x in (tidy_caps(r.street_address), tidy_caps(r.city)) if x), "lat": r.latitude, "lng": r.longitude, "kind": "listing"})
    from app.models.database import MarketArea, AgentMarketArea
    try:
        towns = [n for (n,) in (await db.execute(select(MarketArea.name).join(AgentMarketArea, AgentMarketArea.area_id == MarketArea.id)
                                                  .where(AgentMarketArea.owner_id == user.id).order_by(AgentMarketArea.sort_order.nulls_last()).limit(6))).all()]
    except Exception:  # noqa: BLE001 - towns are a nice-to-have here
        await db.rollback()
        towns = []
    for t in towns:
        c = await _town_center(db, t)
        if c:
            centers.append(c)
    seen, uniq = set(), []
    for c in centers:
        k = c["label"].lower()
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    miles = a.get("miles") if isinstance(a.get("miles"), (int, float)) and 1 <= a["miles"] <= 100 else (prefs.get("miles") or 10)
    # Listing agents the search hides ("exclude") or shows only ("include") - the Discovery page's
    # Exclude Agents / Only Agents (owner, 11/01: "take off Lavinia as one of the agents").
    before = [str(n) for n in (prefs.get("excluded_agents") or []) if str(n).strip()]
    mode = a.get("agent_mode") if a.get("agent_mode") in ("exclude", "include") else (prefs.get("agent_filter_mode") or "exclude")
    drop = [_s(x).lower() for x in (a.get("remove_agents") or []) if _s(x)]
    agents = [n for n in before if not any(d in n.lower() for d in drop)]
    # A name to take off that isn't in their list: the card says so (never "removed" when nothing was).
    missing = [_s(x) for x in (a.get("remove_agents") or []) if _s(x) and not any(_s(x).lower() in n.lower() for n in before)]
    for n in (a.get("add_agents") or []):
        n = _s(n)
        if n and not any(n.lower() == x.lower() for x in agents):
            agents.append(n)
    also = [line for line in ((f"Only from: {', '.join(prefs['brokerages'])}" if prefs.get("brokerages") else ""), (f"Only in: {', '.join(prefs['cities'])}" if prefs.get("cities") else ""),
                              (f"School districts: {', '.join(prefs['school_districts'])}" if prefs.get("school_districts") else "")) if line]
    payload = {"centers": uniq[:8], "center": "" if unresolved or not uniq else uniq[0]["label"], "miles": miles, "unresolved": unresolved, "also": also,
               "was": {"center": prefs.get("landmark_address") or "", "miles": prefs.get("miles"), "min_price": prefs.get("min_price"), "max_price": prefs.get("max_price"),
                       "min_bedrooms": prefs.get("min_bedrooms"), "agents": before, "agent_mode": prefs.get("agent_filter_mode") or "exclude"},
               "agents": agents, "agents_known": before + [n for n in agents if n not in before], "agent_mode": mode,
               "min_price": a.get("min_price") if isinstance(a.get("min_price"), (int, float)) else prefs.get("min_price"),
               "max_price": a.get("max_price") if isinstance(a.get("max_price"), (int, float)) else prefs.get("max_price"),
               "min_bedrooms": a.get("min_bedrooms") if isinstance(a.get("min_bedrooms"), int) else prefs.get("min_bedrooms"),
               "had": bool(prefs.get("landmark_address"))}
    title = "Change your Discovery search" if payload["had"] else "Set up your Discovery search"
    return Prepared(title, [f"Now: {prefs['landmark_address']} · {prefs.get('miles') or 10} mi" if payload["had"] else "Pick where to look - it saves to your Discovery page"]
                    + ([f"Taken off your {'only-these-agents list' if mode == 'include' else 'hidden agents'}: {', '.join(n for n in before if n not in agents)}" + (" - no agent filter now, every agent's listings show" if not agents else "")] if any(n not in agents for n in before) else [])
                    + ([f"Not in your agent filter (nothing to remove): {', '.join(missing)}. Your list: {', '.join(before) or 'no agents'}"] if missing else []),
                    payload, confirm_label="Save my search", picker="discovery")


def _adjust_discovery(user, payload: dict, picks: dict) -> dict:
    out = dict(payload)
    typed = picks.get("custom_center") if isinstance(picks.get("custom_center"), dict) else None
    if typed and _s(typed.get("label")) and isinstance(typed.get("lat"), (int, float)) and isinstance(typed.get("lng"), (int, float)) and -90 <= typed["lat"] <= 90 and -180 <= typed["lng"] <= 180:
        c = {"label": _s(typed["label"])[:300], "lat": typed["lat"], "lng": typed["lng"], "kind": "home"}
        out["centers"] = [c, *[x for x in out.get("centers") or [] if x["label"] != c["label"]]]
    labels = {c["label"] for c in out.get("centers") or []}
    if picks.get("center") in labels:
        out["center"] = picks["center"]
    out["clear_also"] = picks.get("clear_also") is True
    if isinstance(picks.get("miles"), (int, float)) and 1 <= picks["miles"] <= 100:
        out["miles"] = picks["miles"]
    for k in ("min_price", "max_price", "min_bedrooms"):
        if k in picks:
            v = picks.get(k)
            out[k] = v if isinstance(v, (int, float)) and v > 0 else None
    if isinstance(picks.get("agents"), list):
        known = {n.lower(): n for n in out.get("agents_known") or []}
        out["agents"] = [known[str(n).lower()] for n in picks["agents"] if str(n).lower() in known]
    if picks.get("agent_mode") in ("exclude", "include"):
        out["agent_mode"] = picks["agent_mode"]
    return out


async def _r_discovery(db, user, p):
    c = next((x for x in p.get("centers") or [] if x["label"] == p.get("center")), None)
    if c is None:
        raise ActionError("Pick where to center the search first.")
    # Only what changed on this card: a change made on the Discovery page after the card opened stays.
    was = p.get("was") or {}
    new = {"miles": p.get("miles") or 10, "min_price": p.get("min_price"), "max_price": p.get("max_price"), "min_bedrooms": p.get("min_bedrooms")}
    patch = {k: v for k, v in new.items() if k not in was or v != was.get(k)}
    if not was or c["label"] != was.get("center"):
        patch.update(landmark_address=c["label"], latitude=c["lat"], longitude=c["lng"])
    if list(p.get("agents") or []) != list(was.get("agents") or []):
        patch["excluded_agents"] = list(p.get("agents") or [])
    if (p.get("agent_mode") or "exclude") != was.get("agent_mode"):
        patch["agent_filter_mode"] = p.get("agent_mode") or "exclude"
    if p.get("clear_also"):
        patch.update(brokerages=[], cities=[], school_districts=[])
    if patch:
        await _run_route(user, "PATCH", "/api/discovery/preferences", patch)
    return {"message": f"Saved - your Discovery now looks within {p.get('miles') or 10} miles of {c['label']}. Ask me \"show me homes to host\" to see the new list.",
            "link": "/discovery"}

_simple("set_discovery_search", "Set up or change the agent's Discovery search (where they look for homes to host): the center (one of their listings, a town, or ANY address such as their own home, so they can search by commute; pass what they typed in center and the card finds it), "
        "how many miles, an optional price range (min and max) / minimum beds, and the listing agents it hides or shows only (Exclude Agents / Only Agents) - saved to their Discovery page. "
        "Use it for ANY change to their Discovery filter ('look around Wayne instead', '$1.5M to $5M', 'take Lavinia off', 'hide Compass agents' -> add_agents with the names) and when find_homes_to_host says their search area isn't set. "
        "Their current center and miles stay unless they name new ones - pass only what changes (remove_agents takes part of a name, e.g. 'Lavinia'). Set it up right away; the card shows the rest and they tap Save.",
        _obj({"center": {"type": "string"}, "miles": {"type": "number"}, "min_price": {"type": "number"}, "max_price": {"type": "number"}, "min_bedrooms": {"type": "integer"},
              "remove_agents": {"type": "array", "items": {"type": "string"}}, "add_agents": {"type": "array", "items": {"type": "string"}},
              "agent_mode": {"type": "string", "enum": ["exclude", "include"]}}),
        _p_discovery, _r_discovery, MLS, adjust=_adjust_discovery)


# 11/01 additions (capability review): forms/invites follow-through, hosting, blogs, account items.
from app.services.ai import ai_actions_forms, ai_actions_media, ai_actions_more  # noqa: E402,F401
