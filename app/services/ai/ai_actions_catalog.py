"""The AI helper's actions (10/30) - each a `prepare` (checks the request against the agent's own
data and writes the Confirm card; changes nothing) and a `run` (only after the agent taps
Confirm). Every run calls the SAME route the page's own button calls, as the agent, through
ai_route_bridge - so plan, broker-authorization, ownership and validation rules are the app's own.

Rules every action here follows (see ai_actions.py): no deletes, no billing, nothing paid without
saying so, and anything that emails or notifies a real person says so on the card. Left out on
purpose: Media Lab and CMA AI analysis (real money per run - the agent starts those on the page),
publishing the IDX website draft (no undo), connecting calendars/mailboxes (needs the agent's own
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
        body["calendar"] = a["calendar"]; lines.append(f"Put {lead.name}'s follow-ups on my calendar: {_yn(a['calendar'])}")
        if a["calendar"]:
            body["in_app"] = True
    if isinstance(a.get("keep_followups_in_app"), bool) and not body.get("calendar"):
        body["in_app"] = a["keep_followups_in_app"]
        lines.append("Keep their follow-ups in OpenHousePal" if a["keep_followups_in_app"] else "Stop making follow-ups for them")
    if isinstance(a.get("email_on_likes"), bool):
        body["emails"] = a["email_on_likes"]; lines.append(f"Email me when they like or comment: {_yn(a['email_on_likes'])}")
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
    return Prepared(f"{'Turn back on' if on else 'Turn off'} automatic emails to {email}",
                    ["Showcase updates and other automatic emails to this client" + (" start again" if on else " stop until you turn them back on")],
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
    if _s(a.get("notes")):
        body["notes"] = _s(a["notes"]); lines.append(f"Notes: {body['notes'][:200]}")
    if not body:
        raise ActionError("What should change on that follow-up?")
    return Prepared(f"Follow-up: {t.title}", lines, {"task_id": t.id, "body": body})


async def _r_edit_task(db, user, p):
    await _run_route(user, "PATCH", f"/tasks/{p['task_id']}", p["body"])
    return {"message": "Saved.", "link": f"/leads/tasks?task={p['task_id']}"}

_simple("update_followup", "Check off, reopen, reschedule or rename one follow-up. Use task ids from list_followups.",
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
    if "min_price" in body or "max_price" in body:
        lines.append(f"Price: {('$' + format(body['min_price'], ',')) if 'min_price' in body else 'any'} - {('$' + format(body['max_price'], ',')) if 'max_price' in body else 'any'}")
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
    return Prepared(f"Add a home to {_sc_name(c)}", [prop.street_address or prop.full_address or key, "Marked Recommended, shows as new for them"],
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
    d, t = _date_ok(a.get("date")), _s(a.get("time"))
    if not t:
        raise ActionError("What time is the tour?")
    return Prepared(f"Confirm {tour.visitor_name or 'the'} tour", [f"{_nice_date(d)} at {t}", "Only confirm once it's booked and the listing agent accepted"],
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
        lines.append({"on": "Guest follow-ups: on (and on my calendar)", "no_calendar": "Guest follow-ups: in OpenHousePal only", "off": "Guest follow-ups: off"}[a["followups"]])
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
    new = f"{oh.notes.rstrip()}\n{text}" if _s(oh.notes) else text
    return Prepared(f"Add to your note on {_oh_name(oh)}", [f"“{text[:400]}”", "Added below what's already there"], {"open_house_id": oh.id, "notes": new})


async def _r_oh_note(db, user, p):
    await _run_route(user, "PUT", f"/api/open-houses/{p['open_house_id']}/note", {"notes": p["notes"]})
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
    return Prepared(f"Ask your team to host {prop.street_address}", [when] + ([f"Message: “{body['message']}”"] if body["message"] else []), {"body": body},
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
    return Prepared(f"Ask to host {prop.street_address}", [f"{_nice_date(d)}, {_nice_time(s)}-{_nice_time(e)}", f"Listing agent: {prop.list_agent_full_name or 'the listing agent'}"]
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
    who = await _agent_name(db, r.listing_owner_agent_id)
    yes = bool(a.get("accept"))
    when = f"{_nice_date(str(r.counter_date))}, {_nice_time(str(r.counter_start_time))}-{_nice_time(str(r.counter_end_time))}" if r.counter_date else "their suggested time"
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

async def _p_cma(db, user, a):
    key = _need_id(a, "listing_key")
    prop = await _one(db, select(Property).where(Property.listing_key == key), "I couldn't find that home.")
    mode = "buyer" if a.get("for_buyer") else "listing"
    # AI or manual is asked ON the card (owner, 11/01) - two buttons, not a chat question. What one AI
    # run usually costs, as a share of their monthly AI allowance, is shown on the AI choice.
    try:
        est = await _run_route(user, "GET", "/api/cma/ai-estimate")
    except ActionError:
        est = {}
    addr = tidy_caps(prop.street_address) if prop.street_address else key
    return Prepared(f"Start a CMA for {addr}", [f"{'Buyer / Offer' if mode == 'buyer' else 'Seller / Listing'} CMA"] +
                    ([f"Client: {_s(a.get('client_name'))}"] if _s(a.get("client_name")) else []),
                    {"listing_key": key, "mode": mode, "client_name": _s(a.get("client_name")) or None, "ai": True, "address": addr,
                     "ai_percent": (est or {}).get("percent"), "photo": (prop.photos or [None])[0] if isinstance(prop.photos, list) and prop.photos else None},
                    confirm_label="Start", picker="cma")


def _adjust_cma(user, payload: dict, picks: dict) -> dict:
    out = {**payload, "ai": bool(picks.get("ai", payload.get("ai")))}
    if picks.get("mode") in ("listing", "buyer"):
        out["mode"] = picks["mode"]
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
    subject = await _run_route(user, "GET", f"/api/cma/subject-from-listing/{p['listing_key']}")
    out = await _run_route(user, "POST", "/api/cma", {"mode": p["mode"], "client_name": p["client_name"], "subject": subject})
    rid = (out or {}).get("id")
    link = f"/studio/cma/{rid}" if rid else "/studio/cma"
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

_simple("start_cma", "Start a CMA for a home (listing_key from search_properties), seller/listing or buyer. The card asks AI CMA (it picks the comps and "
        "runs the AI analysis for them - uses AI allowance) or Manual (they pick comps), and Seller or Buyer - don't ask either in chat (pass for_buyer only if they said).",
        _obj({"listing_key": {"type": "string"}, "for_buyer": {"type": "boolean"}, "client_name": {"type": "string"}}, ["listing_key"]), _p_cma, _r_cma, MLS,
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
               "brand_color": kit.get("secondary_color") or "#C9A24D"}
    return Prepared(f"Make a Spotlight for {tidy_caps(prop.street_address)}",
                    ["Made from your listing's photos and details - I write the highlights, the story and the town section for you",
                     "Saved as a draft - nothing goes live until you publish"]
                    + (["You already have a Spotlight for this home - this makes another"] if have else []),
                    {"listing_key": key, "template_id": style, "preview": preview}, picker="spotlight")


def _adjust_spotlight(user, payload: dict, picks: dict) -> dict:
    out = dict(payload)
    if picks.get("template_id") in ("cinematic_luxury", "editorial_estate"):
        out["template_id"] = picks["template_id"]
    color = picks.get("brand_color")
    if color and color != (payload.get("preview") or {}).get("brand_color"):
        if not _HEX.match(str(color)):
            raise ActionError("That color isn't one I can use.")
        out["brand_color"] = str(color)   # a Spotlight wears the Branding color - this changes it
    return out


async def _r_spotlight(db, user, p):
    if p.get("brand_color"):
        await _run_route(user, "PUT", "/studio/brand-kit", {"secondary_color": p["brand_color"]})
    out = await _run_route(user, "POST", "/studio/property-sites", {"listing_key": p["listing_key"], "template_id": p["template_id"]})
    sid = (out or {}).get("id")
    # The card then runs the editor's own first-open setup (amenities, highlights, short story,
    # town) right in the chat - see components/assistant/spotlightSetup.ts. If they close the chat
    # first, the editor still finishes it the first time it's opened.
    return {"message": "Spotlight made." + (" Your brand color changed too." if p.get("brand_color") else ""),
            "link": f"/studio/property-sites/{sid}" if sid else "/studio/property-sites",
            "made": {"kind": "spotlight", "site_id": sid} if sid else None}

_simple("create_spotlight", "Make a Listing Spotlight (single-listing website) for one of the agent's own active listings (listing_key from search_properties). "
        "The card shows a small preview with the look (Luxe / Editorial) and color to pick, then writes the highlights, story and town itself - "
        "don't ask the agent anything about the house. IDX plan.",
        _obj({"listing_key": {"type": "string"}, "style": {"type": "string", "enum": ["luxe", "editorial"]}}, ["listing_key"]), _p_spotlight, _r_spotlight, IDX,
        adjust=_adjust_spotlight)


async def _p_spotlight_pub(db, user, a):
    sid = _need_id(a, "spotlight_id")
    site = await _one(db, select(PropertyWebsite).where(PropertyWebsite.id == sid, PropertyWebsite.owner_id == user.id), "I couldn't find that Spotlight.")
    addr = ((site.property_snapshot or {}).get("street_address") or (site.property_snapshot or {}).get("address") or "this home")
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
        share = {"url": url, "preview": url} if url else None   # its own address - the preview reads it there
    return {"message": "Published." if p["publish"] else "Taken down.", "link": f"/studio/property-sites/{p['spotlight_id']}", "share": share}

_simple("publish_spotlight", "Publish (publish=true) or take down (false) one of the agent's Listing Spotlights (spotlight_id from list_my_spotlights).",
        _obj({"spotlight_id": {"type": "string"}, "publish": {"type": "boolean"}}, ["spotlight_id", "publish"]), _p_spotlight_pub, _r_spotlight_pub, IDX)

_HEX = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


async def _p_brand(db, user, a):
    body, lines = {}, []
    for k, label in (("primary_color", "Main color"), ("secondary_color", "Second color"), ("accent_color", "Accent color")):
        if _s(a.get(k)):
            if not _HEX.match(_s(a[k])):
                raise ActionError("Colors need to be hex codes like #1F3A5F.")
            body[k] = _s(a[k]); lines.append(f"{label}: {body[k]}")
    if a.get("look") in ("classic", "luxe"):
        if a["look"] == "luxe" and user.plan_tier != "IDX" and not user.is_admin:
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
               "secondary_color": kit.get("secondary_color") or "#C9A24D", "idx": user.plan_tier == "IDX" or bool(user.is_admin)}
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
        return Prepared("Your Branding", ["Pick a look, color and Ivory or Midnight - your hub, forms, signature and website wear it"],
                        {"body": {}, "current": current},
                        confirm_label="Save", picker="brand")
    return Prepared("Update your Branding", lines + ["Your hub, forms, signature and website wear it"], {"body": body, "current": current},
                    picker="brand")


def _adjust_brand(user, payload: dict, picks: dict) -> dict:
    """The swatches tapped on the card -> the Branding update (same rules as the Branding page)."""
    body = dict(payload.get("body") or {})
    look = dict(body.get("look") or {})
    if picks.get("theme") in ("classic", "luxe"):
        if picks["theme"] == "luxe" and user.plan_tier != "IDX" and not user.is_admin:
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
    apply_all = bool(picks.get("apply_all"))
    if not body and not apply_all:
        raise ActionError("Pick something to change first.")
    return {**payload, "body": body, "apply_all": apply_all}


async def _r_brand(db, user, p):
    if not p.get("body") and not p.get("apply_all"):
        raise ActionError("Pick something to change first.")
    # The same saves as the Branding page: the brand kit, then (if they said so) "Use this color for
    # all features" - so Branding, Settings and every tool show the same thing.
    if p.get("body"):
        await _run_route(user, "PUT", "/studio/brand-kit", p["body"])
    if p.get("apply_all"):
        await _run_route(user, "POST", "/studio/brand-kit/apply-all", {})
        return {"message": "Branding saved and every tool uses it now - your hub, forms, signature, invites and website. Logos, fonts and your disclaimer are on the Branding page.",
                "link": "/studio/brand-kit"}
    return {"message": "Branding saved - every tool set to your brand wears it now. Logos, fonts and your disclaimer are on the Branding page.",
            "link": "/studio/brand-kit"}

register({"name": "update_branding", "description": (
    "Change the agent's Branding (brand kit). For 'change my colors / show me my options / change my look / put my colors back / undo' call it WITHOUT colors - "
    "the card shows every color, the Classic/Luxe look, the Luxe finish and Ivory/Midnight for them to tap, exactly like the Branding page. "
    "Only pass hex colors or look when they named exact ones."),
    "input_schema": _obj({"primary_color": {"type": "string"}, "secondary_color": {"type": "string"}, "accent_color": {"type": "string"}, "look": {"type": "string", "enum": ["classic", "luxe"]}})},
    _p_brand, _r_brand, None, adjust=_adjust_brand)


Q_TYPES = {"short_text": "Short answer", "long_text": "Long answer", "select": "Pick one", "multi_select": "Pick any",
           "number": "Number", "currency": "Dollar amount", "date": "Date", "time": "Time", "email": "Email", "phone": "Phone"}

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


def _form_questions(raw) -> tuple[list, list]:
    """Questions as the model or the card sends them -> the form's questions + the card's lines."""
    qs, lines = [], []
    if isinstance(raw, str):   # the model sometimes sends the list as JSON text
        try:
            import json
            raw = json.loads(raw)
        except ValueError:
            raw = [ln for ln in raw.splitlines() if ln.strip()]
    if not isinstance(raw, list):
        raw = []
    for i, q in enumerate(raw[:15]):
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
        qs.append({"id": f"ai_q{i + 1}", "type": qt, "label": label[:200], "required": bool(q.get("required")),
                   **({"options": opts} if qt in ("select", "multi_select") else {})})
        lines.append(f"{label} ({Q_TYPES[qt]}{': ' + ', '.join(opts) if opts else ''}){' - required' if q.get('required') else ''}")
    return qs, lines


async def _p_form(db, user, a):
    title = _s(a.get("title"))
    if not title:
        raise ActionError("What should the form be called?")
    qs, _qlines = _form_questions(a.get("questions") or [])
    lines = ["Starts with Full name, Email and Phone"]   # the questions themselves are a step on the card
    # The two look choices are taps on the card, not chat questions (owner, 11/01: "buttons, much less
    # words"): moving background on/off, and one of their own active listings to link (or none).
    from app.utils.plan_ids import listing_owner_key
    mine = listing_owner_key(user)
    homes = []
    if mine:
        rows = (await db.execute(select(Property).where(
            or_(Property.list_agent_mls_id == mine, Property.co_list_agent_mls_id == mine),
            Property.home_status.in_(("ACTIVE-BRIGHT", "COMING SOON-BRIGHT"))).order_by(Property.mls_list_date.desc().nulls_last()).limit(6))).scalars().all()
        homes = [{"listing_key": r.listing_key, "address": tidy_caps(r.street_address) or "", "city": tidy_caps(r.city) or "",
                  "photo": (r.photos[0] if isinstance(r.photos, list) and r.photos and isinstance(r.photos[0], str) else None)} for r in rows]
    key = _s(a.get("listing_key"))
    payload = {"title": title[:120], "questions": qs, "homes": homes, "bank": FORM_QUESTION_BANK,
               "background_effects": a["background_effects"] if isinstance(a.get("background_effects"), bool) else True,
               "listing_key": key if any(h["listing_key"] == key for h in homes) or not key else key}
    return Prepared(f"Make a new form: {title}", lines + ["You can change the look and add more in the form editor"], payload, picker="form")


def _adjust_form(user, payload: dict, picks: dict) -> dict:
    out = dict(payload)
    if isinstance(picks.get("questions"), list):
        out["questions"], _ = _form_questions(picks["questions"])
    if isinstance(picks.get("background_effects"), bool):
        out["background_effects"] = picks["background_effects"]
    if "listing_key" in picks:
        out["listing_key"] = _s(picks.get("listing_key")) or None
    return out


async def _r_form(db, user, p):
    if _s(p.get("listing_key")):
        prop = await _one(db, select(Property).where(Property.listing_key == p["listing_key"]), "I couldn't find that home in the listings.")
        photos = prop.photos if isinstance(getattr(prop, "photos", None), list) else []
        p = {**p, "linked_property": {k: v for k, v in {
            "listing_key": p["listing_key"], "address": prop.street_address, "city": prop.city, "state": prop.state, "zipcode": getattr(prop, "zipcode", None),
            "price": prop.price, "beds": getattr(prop, "bedrooms", None), "baths": getattr(prop, "bathrooms", None), "sqft": getattr(prop, "living_area", None),
            "image_url": photos[0] if photos and isinstance(photos[0], str) else None}.items() if v is not None}}
    out = await _run_route(user, "POST", "/agent-hub/forms", {"title": p["title"]})
    fid = (out or {}).get("id")
    update = {k: p[k] for k in ("background_effects", "linked_property") if k in p}
    if fid and p.get("questions"):
        update["questions"] = list((out or {}).get("questions") or []) + p["questions"]
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

_simple("create_form", "Make a new custom form (Forms & Invites) with a title and the agent's own questions (types: " + ", ".join(Q_TYPES) + "; select/multi_select need options). Full name, email and phone are always there already. "
        "Set it up right away with a few fitting questions - the card itself lets them switch questions on/off, tap common ones in and type their own, plus the moving-background switch and their listings to link (don't ask about any of those in chat). Pass listing_key only if they named the home.",
        _obj({"title": {"type": "string"}, "questions": {"type": "array", "items": {"type": "object", "properties": {
            "label": {"type": "string"}, "type": {"type": "string", "enum": list(Q_TYPES)}, "options": {"type": "array", "items": {"type": "string"}}, "required": {"type": "boolean"}}}},
            "background_effects": {"type": "boolean"}, "listing_key": {"type": "string"}},
             ["title"]), _p_form, _r_form, adjust=_adjust_form)

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
    options = {"homes": homes, "host": host, "luxe_ok": bool(luxe_theme_allowed(user)),
               "brand_accent": kit.get("secondary_color") or "#C9A24D", "brand_metal": look.get("metal") if look.get("theme") == "luxe" else None,
               "kind_given": bool(kind)}
    return Prepared("Make an event invite", lines + ["It gets its own link - nobody receives it until you send it from Forms & Invites"],
                    {"body": body, "options": options}, confirm_label="Make it", picker="invite")


async def _r_invite(db, user, p):
    out = await _run_route(user, "POST", "/invites", p["body"])
    code = (out or {}).get("code")
    share = await _share(db, user, f"/i/{code}", url=(out or {}).get("share_url")) if code else None
    iid = (out or {}).get("id")
    return {"message": "Invite made - open it to add a cover photo, pick who to send it to and send it.",
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
    return {**payload, "body": body}

register({"name": "create_invite", "description": (
    "Make an event invite with RSVP (open house, broker's open, client event, other). Set it up as soon as they ask - the card itself asks, as taps with a live preview, "
    "the kind, which of their listings, the date, the times, the design and the color (Luxe on IDX) - so never ask those in chat; pass only what they already said "
    "(kind, title, date YYYY-MM-DD, times HH:MM, message). place_name is a venue's NAME (a club, a restaurant) - never the street address; "
    "for a client event away from their listings ask where in one short question. Never sends it."),
    "input_schema": _obj({"kind": {"type": "string", "enum": list(INVITE_TYPES)}, "title": {"type": "string"}, "date": {"type": "string"}, "start_time": {"type": "string"}, "end_time": {"type": "string"},
                          "place_name": {"type": "string"}, "address": {"type": "string"}, "message": {"type": "string"}, "design": {"type": "string", "enum": list(INVITE_DESIGNS)}})},
    _p_invite, _r_invite, None, adjust=_adjust_invite)


BLOG_TONES = ("warm", "professional", "luxury", "energetic", "straight")
BLOG_LENGTHS = ("short", "standard", "long")
BLOG_AUDIENCES = ("Buyers", "Sellers", "First-time buyers", "Move-up buyers", "Luxury buyers", "Investors", "Relocating", "Homeowners")
BLOG_CTAS = ("how_can_i_help", "home_value", "showing", "buying", "selling", "make_me_sell", "question")


def _money_short(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    return f"${v / 1e6:.2f}".rstrip("0").rstrip(".") + "M" if v >= 1e6 else f"${round(v / 1e3)}K"


async def _p_blog(db, user, a):
    topic = _s(a.get("topic"))
    if not topic:
        raise ActionError("What should the blog post be about?")
    # The Blogs page's own questions, as taps on the card (owner, 11/01: "ask the steps in chat - look
    # and options"): who it's for, towns (+ Town Spotlight), your sales, tone, length, the ending
    # button, key points / your take - and each length's share of the AI allowance.
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
    sales = [{"id": str(x.get("id")), "label": ", ".join(v for v in (x.get("address"), x.get("city")) if v) + (f" \u00b7 {_money_short(x.get('price'))}" if x.get("price") else "")}
             for x in (ctx.get("recent_sales") or [])[:8] if x.get("id")]
    want_areas = [t for t in (a.get("areas") or []) if isinstance(t, str) and t.strip()][:5]
    body = {"topic": topic[:500], "tone": a.get("tone") if a.get("tone") in BLOG_TONES else "warm",
            "length": a.get("length") if a.get("length") in BLOG_LENGTHS else "standard",
            "audiences": [x for x in (a.get("audiences") or []) if x in BLOG_AUDIENCES],
            "areas": want_areas, "cta": a.get("cta") if a.get("cta") in BLOG_CTAS else "how_can_i_help",
            "area_slugs": [cards[t.lower()] for t in want_areas if t.lower() in cards]}
    if _s(a.get("key_points")):
        body["key_points"] = _s(a["key_points"])[:2000]
    return Prepared("Write a blog post with AI", [f"Topic: {topic[:200]}", "Saved as a draft - never published until you do"],
                    {"body": body, "percents": percents, "options": {"areas": areas, "area_cards": cards, "sales": sales}},
                    warning="This uses your monthly AI allowance.", confirm_label="Write it", picker="blog")


def _adjust_blog(user, payload: dict, picks: dict) -> dict:
    out = dict(payload)
    body = dict(out.get("body") or {})
    opts = out.get("options") or {}
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
    for k in ("key_points", "personal_take"):
        if k in picks:
            v = _s(picks.get(k))[:2000]
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
    # It writes in the background (research, writing, fact-check): the card keeps working and the
    # chat says when the draft is ready - like an AI CMA (owner, 11/01).
    from app.services.ai import ai_notify
    from app.services.ai.ai_actions import CARD
    await ai_notify.watch(db, user, f"blog:{bid}", CARD.get() or {}, _s((p.get("body") or {}).get("topic"))[:80] or "your topic")
    return {"working": True, "link": f"/studio/blogs/{bid}",
            "message": "Writing your draft now - researching, writing and fact-checking takes a few minutes. I'll tell you right here when it's "
                       "ready, even if you close the site."}

_simple("write_blog_post", "Start the AI blog writer on a topic (draft only; uses the agent's AI allowance). Plans with Blogs. "
        "You CAN write blog posts - never say otherwise. The card asks the Blogs page's own questions as taps (who it's for, towns + Town Spotlight, their sales to mention, tone, length, the ending button, key points / their take) - set it up as soon as you have the topic; never ask those in chat (pass them only if they already said).",
        _obj({"topic": {"type": "string"}, "key_points": {"type": "string"}, "tone": {"type": "string", "enum": list(BLOG_TONES)},
              "length": {"type": "string", "enum": list(BLOG_LENGTHS)}, "audiences": {"type": "array", "items": {"type": "string", "enum": list(BLOG_AUDIENCES)}},
              "areas": {"type": "array", "items": {"type": "string"}, "description": "Towns the article is about, if they named any (up to 5)."},
              "cta": {"type": "string", "enum": list(BLOG_CTAS)}}, ["topic"]), _p_blog, _r_blog, STUDIO, adjust=_adjust_blog)


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

_simple("update_profile", "Change the agent's profile: name, title, brokerage, brokerage phone (print ads need it), team, market, bio (<=280 chars), call/text numbers (phone_call is their mobile on designs), contact email. Their headshot can't be added from chat - send them to Settings.",
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
    lines = [kind, f"Look: {DESIGN_LAYOUTS.get(layout, 'the recommended layout')}, {DESIGN_COLORS[color]} colors"]
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
        payload["headline"] = _s(a["headline"])[:90]; lines.append(f"Headline: “{payload['headline']}”")
    lines.append("Made in your Studio - you'll see it here with a Download button, and can still edit it")
    return Prepared(f"Make {'an' if kind[:1].lower() in 'aeiou' else 'a'} {kind}", lines, payload, confirm_label="Make it")

register({"name": "create_design", "description": (
    "Make a Studio design (flyer, postcard, door hanger, social post...) that appears in the chat with a Download button. "
    "situation: " + ", ".join(DESIGN_SITUATIONS) + ". format must be one the situation offers: "
    + "; ".join(f"{k}: {', '.join(v[2])}" for k, v in DESIGN_SITUATIONS.items())
    + ". just_listed/coming_soon/price_improvement/under_contract need one of the agent's own listings (listing_key); just_sold a sale they had; open_house their listing or one they're approved to host (+ optional date/time). "
    "The others (home_valuation, make_me_sell, find_your_dream_home, social_ad) need no home - optional headline. layout: keepsake, billboard, slant, postage, filmstrip, luxe (IDX). colors: light (Ivory), stone, dark (Charcoal), forest, brand, luxe (IDX). "
    "The card itself lets the agent pick the layout and colors before tapping Make it - don't ask about the look first; pass layout/colors only when they named one. "
    "The card also lists their other listings (or sales) to switch to. When they say it's a different / another listing, never offer the same home again: use the one they name, or list their listings and ask which. "
    "Use the listing_key from their listings/sales list in your instructions - never a search result for a look-alike address."),
    "input_schema": _obj({"situation": {"type": "string", "enum": list(DESIGN_SITUATIONS)}, "format": {"type": "string", "enum": list(DESIGN_FORMATS)},
                          "listing_key": {"type": "string"}, "layout": {"type": "string", "enum": list(DESIGN_LAYOUTS)}, "colors": {"type": "string", "enum": list(DESIGN_COLORS)},
                          "headline": {"type": "string"}, "open_house_date": {"type": "string"}, "start_time": {"type": "string"}, "end_time": {"type": "string"}},
                         ["situation", "format"])},
         _p_design, None, STUDIO, client="create_design")
