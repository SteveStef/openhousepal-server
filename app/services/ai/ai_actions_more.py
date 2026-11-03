"""ai_actions_more - filled in 11/01 (AI helper additions from the capability review). Loaded at the end of ai_actions_catalog.py.

Same rules as the catalog: `prepare` only reads (the agent's own rows, or the page's own GET) and
writes the card; `run` calls the SAME route the page's button calls, as the agent. Anything that
tells another person says so on the card. Kept manual on purpose: deletes, money, sign-ins and
keys, publishing the IDX draft, the hub address and a new forwarding address.
"""
from datetime import date as _date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import select

from app.models.database import (
    Blog, OpenHouseEvent, OpenHouseHostRequest, OpenHouseHostRequestEvent, OpenHouseTeamBroadcast, OpenHouseTeamMember,
    OpenHouseVisitor, Property, SupportTicket, User,
)
from app.services.ai.ai_actions import ActionError, Prepared
from app.services.email_service import tidy_caps
from app.services.ai.ai_actions_catalog import (
    MLS, _agent_name, _date_ok, _need_id, _nice_date, _nice_time, _obj, _oh_name, _one, _run_route, _s, _simple, _time_ok, _yn,
)

STUDIO = {"PREMIUM", "IDX", "PRO_NO_MLS"}     # Blogs
SIGNATURE = {"IDX", "PRO_NO_MLS"}             # Email signature


def _slot(d: Any, s: Any = None, e: Any = None) -> str:
    out = _nice_date(str(d)) if d else "that day"
    return out + (f", {_nice_time(str(s))}-{_nice_time(str(e))}" if s and e else "")


def _times(a: dict, *, required: bool = True) -> tuple[Optional[str], Optional[str]]:
    if not _s(a.get("start_time")) and not _s(a.get("end_time")) and not required:
        return None, None
    s, e = _time_ok(a.get("start_time")), _time_ok(a.get("end_time"))
    if e <= s:
        raise ActionError("The end time needs to be after the start time.")
    return s, e


def _future_day(v: Any) -> str:
    d = _date_ok(v)
    if d < _date.today().isoformat():
        raise ActionError("That day has already passed.")
    return d


async def _address(db, listing_key: str, open_house_id: Optional[str] = None) -> str:
    p = (await db.execute(select(Property.street_address).where(Property.listing_key == listing_key))).scalar_one_or_none()
    if not p and open_house_id:   # a listing that left the feed - its open house still has the address
        p = (await db.execute(select(OpenHouseEvent.address).where(OpenHouseEvent.id == open_house_id))).scalar_one_or_none()
        p = (p or "").split(",")[0]
    return tidy_caps(p) if p else "that listing"


# ================================================================ HOSTING (Discovery -> My Requests)

async def _my_req(db, user, a) -> OpenHouseHostRequest:
    return await _one(db, select(OpenHouseHostRequest).where(OpenHouseHostRequest.id == _need_id(a, "request_id"),
                                                           OpenHouseHostRequest.requesting_agent_id == user.id), "I couldn't find that hosting request.")


async def _p_withdraw(db, user, a):
    r = await _my_req(db, user, a)
    if r.status not in ("pending", "countered", "approved"):
        raise ActionError("That request is already closed - there's nothing to withdraw.")
    if r.requested_date and str(r.requested_date)[:10] < _date.today().isoformat():
        raise ActionError("That open house day has already passed - there's nothing to back out of.")
    who = await _agent_name(db, r.listing_owner_agent_id)
    addr = await _address(db, r.listing_key, r.open_house_event_id)
    when = _slot(r.requested_date, r.requested_start_time, r.requested_end_time)
    if r.status == "approved":
        return Prepared(f"Back out of hosting {addr}", [when, "Frees the day so they can find another host", "Takes it off your calendar"],
                        {"request_id": r.id}, warning=f"This emails {who} right away that you can't host.", confirm_label="I can't host")
    return Prepared(f"Withdraw your request to host {addr}", [when] + (["Also turns down the other time they suggested"] if r.status == "countered" else []),
                    {"request_id": r.id}, warning=f"This emails {who}.", confirm_label="Withdraw it")


async def _r_withdraw(db, user, p):
    await _run_route(user, "POST", f"/api/open-house-manager/host-requests/{p['request_id']}/withdraw")
    return {"message": "Done - the listing agent has been told.", "link": "/discovery?myRequests=1"}

_simple("withdraw_hosting_request",
        "Take back the agent's own request to host someone else's listing, or back out of an open house they were approved to host "
        "(request_id from get_my_hosting_requests). Use for 'cancel my request to host 12 Oak' / 'I can't host Saturday anymore'.",
        _obj({"request_id": {"type": "string"}}, ["request_id"]), _p_withdraw, _r_withdraw, MLS)


async def _host_used_other_time(db, r: OpenHouseHostRequest) -> bool:
    """Same rule as open_house_manager_service._suggestions_used: one 'Other time' per side."""
    actors = (await db.execute(select(OpenHouseHostRequestEvent.actor_id).where(
        OpenHouseHostRequestEvent.request_id == r.id, OpenHouseHostRequestEvent.action == "countered"))).scalars().all()
    return r.requesting_agent_id in actors or (r.broadcast_id is not None and r.status in ("pending", "countered"))


async def _p_other_time(db, user, a):
    r = await _my_req(db, user, a)
    if r.status != "countered":
        raise ActionError("You can offer another time only after the listing agent suggests one. To change a waiting request, withdraw it and ask again.")
    if await _host_used_other_time(db, r):
        raise ActionError("You already suggested another time once on this request - now it's accept or decline their time.")
    d = _future_day(a.get("date"))
    s, e = _times(a)
    note = _s(a.get("message"))[:500] or None
    who = await _agent_name(db, r.listing_owner_agent_id)
    addr = await _address(db, r.listing_key)
    return Prepared(f"Offer {who} another time for {addr}", [f"Theirs: {_slot(r.counter_date, r.counter_start_time, r.counter_end_time)}",
                                                             f"Yours: {_slot(d, s, e)}", "You can only offer another time once"]
                    + ([f"Note: “{note}”"] if note else []),
                    {"request_id": r.id, "body": {"proposed_date": d, "proposed_start_time": f"{s}:00", "proposed_end_time": f"{e}:00", "note": note}},
                    warning=f"This emails {who}.", confirm_label="Send it")


async def _r_other_time(db, user, p):
    await _run_route(user, "POST", f"/api/open-house-manager/host-requests/{p['request_id']}/counter-back", p["body"])
    return {"message": "Sent - it's back with the listing agent to approve.", "link": "/discovery?myRequests=1"}

_simple("offer_other_host_time",
        "On the agent's own hosting request where the listing agent suggested a different time: offer a different time back (the page's "
        "'Other time'). Date YYYY-MM-DD, start/end HH:MM 24h, optional message. request_id from get_my_hosting_requests. Only once per request.",
        _obj({"request_id": {"type": "string"}, "date": {"type": "string"}, "start_time": {"type": "string"}, "end_time": {"type": "string"},
              "message": {"type": "string"}}, ["request_id", "date", "start_time", "end_time"]), _p_other_time, _r_other_time, MLS)


async def _p_team_ask(db, user, a):
    b = await _one(db, select(OpenHouseTeamBroadcast).where(OpenHouseTeamBroadcast.id == _need_id(a, "ask_id")), "I couldn't find that host ask.")
    direct = b.target_agent_id == user.id
    if b.target_agent_id and not direct:
        raise ActionError("That ask went to another agent.")
    if not direct:
        member = (await db.execute(select(OpenHouseTeamMember.id).where(OpenHouseTeamMember.owner_id == b.owner_id, OpenHouseTeamMember.member_agent_id == user.id,
                                                                      OpenHouseTeamMember.status == "accepted"))).first()
        if not member:
            raise ActionError("I couldn't find that host ask.")
    if b.status != "open":
        raise ActionError("That ask is closed - someone else took it or it was cancelled.")
    if (await db.execute(select(OpenHouseHostRequest.id).where(OpenHouseHostRequest.broadcast_id == b.id, OpenHouseHostRequest.requesting_agent_id == user.id))).first():
        raise ActionError("You already answered that ask.")
    who = await _agent_name(db, b.owner_id)
    addr = await _address(db, b.listing_key)
    asked = _nice_date(str(b.requested_date)) + (", any time" if b.is_anytime else f", {_nice_time(str(b.requested_start_time))}-{_nice_time(str(b.requested_end_time))}")
    if a.get("answer") == "no":
        if not direct:
            raise ActionError(f"There's nothing to send for a no - {who}'s ask went to the whole team, so it just stays open for the others.")
        return Prepared(f"Tell {who} you can't host {addr}", [asked], {"ask_id": b.id, "answer": "no"}, warning=f"This emails {who} right away.",
                        confirm_label="Send it")
    if a.get("answer") != "yes":
        raise ActionError("Yes or no?")
    body: dict = {}
    d = _future_day(a["date"]) if _s(a.get("date")) else str(b.requested_date)
    if b.is_anytime and not (_s(a.get("start_time")) and _s(a.get("end_time"))):
        raise ActionError("It's an any-time ask - what start and end time would you host?")
    s, e = _times(a, required=False)
    changed = d != str(b.requested_date) or (not b.is_anytime and s and (f"{s}:00" != str(b.requested_start_time) or f"{e}:00" != str(b.requested_end_time)))
    if d != str(b.requested_date) or s:
        body = {"proposed_date": d, **({"proposed_start_time": f"{s}:00", "proposed_end_time": f"{e}:00"} if s else {})}
        if not b.is_anytime and not s:
            body.update(proposed_start_time=str(b.requested_start_time), proposed_end_time=str(b.requested_end_time))
    note = _s(a.get("message"))[:500] or None
    if note:
        body["note"] = note
    when = _slot(d, s or b.requested_start_time, e or b.requested_end_time)
    if changed:
        lines = [f"They asked: {asked}", f"You offer: {when}", f"Goes to {who} to approve"]
        msg_warn = f"This emails {who}."
    else:
        lines = [when, "You're booked as soon as you confirm" + ("" if direct else " - first teammate to say yes gets it")]
        msg_warn = f"This tells {who} right away that you're hosting."
    return Prepared(f"Say yes to hosting {addr}", lines + ([f"Note: “{note}”"] if note else []), {"ask_id": b.id, "answer": "yes", "body": body},
                    warning=msg_warn, confirm_label="I'll host")


async def _r_team_ask(db, user, p):
    if p["answer"] == "no":
        await _run_route(user, "POST", f"/api/open-house-manager/team/broadcasts/{p['ask_id']}/decline")
        return {"message": "Done - they've been told.", "link": "/discovery?myRequests=1"}
    out = await _run_route(user, "POST", f"/api/open-house-manager/team/broadcasts/{p['ask_id']}/respond", p.get("body") or {})
    if isinstance(out, dict) and out.get("status") == "approved":
        return {"message": "You're confirmed to host! Build or reuse your kit on My Opens.", "link": "/open-houses"}
    return {"message": "Sent - the listing agent will approve or answer.", "link": "/discovery?myRequests=1"}

_simple("answer_team_host_ask",
        "Answer a teammate's (or a listing agent's direct) ask to host their open house: answer yes or no (ask_id = the id from get_team_host_asks). "
        "Yes as asked books it right away; a different date or time (date YYYY-MM-DD, start/end HH:MM) goes to them to approve; an 'any time' ask needs "
        "start/end times. No only applies to an ask sent just to this agent (a team-wide ask just stays open for the others).",
        _obj({"ask_id": {"type": "string"}, "answer": {"type": "string", "enum": ["yes", "no"]}, "date": {"type": "string"},
              "start_time": {"type": "string"}, "end_time": {"type": "string"}, "message": {"type": "string"}}, ["ask_id", "answer"]),
        _p_team_ask, _r_team_ask, MLS)


# ================================================================ OPEN HOUSE GUESTS (Visitors window)

async def _visitor(db, user, a) -> OpenHouseVisitor:
    return await _one(db, select(OpenHouseVisitor).join(OpenHouseEvent, OpenHouseEvent.id == OpenHouseVisitor.open_house_event_id)
                      .where(OpenHouseVisitor.id == _need_id(a, "visitor_id"), OpenHouseEvent.agent_id == user.id), "I couldn't find that guest.")


async def _p_guest_note(db, user, a):
    v = await _visitor(db, user, a)
    text = _s(a.get("text"))
    if not text:
        raise ActionError("What should the note say?")
    if not v.lead_id:
        raise ActionError(f"{v.full_name or 'That guest'} isn't linked to a lead yet, so I can't add a note - put it in the open house's own note instead.")
    return Prepared(f"Add a note on {v.full_name or 'this guest'}", [f"“{text[:400]}”", "Shows in the Visitors window and on their lead page"],
                    {"visitor_id": v.id, "open_house_id": v.open_house_event_id, "text": text[:4000]})


async def _r_guest_note(db, user, p):
    await _run_route(user, "POST", f"/api/visitors/{p['visitor_id']}/notes", {"text": p["text"]})
    return {"message": "Note added.", "link": f"/open-houses/visitors/{p['open_house_id']}"}

_simple("add_guest_note",
        "Add a note on one open house guest (the Visitors window's Notes - shared with their lead page and it can update their follow-up). "
        "visitor_id from get_open_house_guests or find_open_house_guests. Use for 'note that Matt from Saturday wants a 4-bedroom'.",
        _obj({"visitor_id": {"type": "string"}, "text": {"type": "string"}}, ["visitor_id", "text"]), _p_guest_note, _r_guest_note)


async def _p_share_guests(db, user, a):
    oh = await _one(db, select(OpenHouseEvent).where(OpenHouseEvent.id == _need_id(a, "open_house_id"), OpenHouseEvent.agent_id == user.id,
                                                    OpenHouseEvent.is_deleted.is_(False)), "I couldn't find that open house.")
    if not oh.listing_key:
        raise ActionError("That open house isn't tied to an MLS listing, so there's no listing agent to share with.")
    prop = (await db.execute(select(Property).where(Property.listing_key == oh.listing_key))).scalar_one_or_none()
    if not prop:
        raise ActionError("I couldn't find that listing.")
    mine = bool(user.mls_id) and user.mls_id in (prop.list_agent_mls_id, prop.co_list_agent_mls_id)
    if mine:
        raise ActionError("That's your own listing - there's no other listing agent to share your guests with.")
    la = prop.list_agent_full_name or "the listing agent"
    on = bool(a.get("on"))
    if on == bool(oh.share_guests_with_listing_agent):
        raise ActionError(f"Your guest list is {'already' if on else 'not'} shared with {la}.")
    on_app = bool((await db.execute(select(User.id).where(User.mls_id.in_([m for m in (prop.list_agent_mls_id, prop.co_list_agent_mls_id) if m])).limit(1))).first())
    if on:
        lines = [f"{la} sees each guest's name, email and phone for the days you hosted - under their listing in Opens → Manager",
                 "They always see the guest counts either way"]
        if not on_app:
            lines.append(f"{la} isn't on OpenHousePal yet, so nobody sees it until they join")
        warn = f"No email goes out - {la} just sees the names next time they look. Your guests stay your leads."
    else:
        lines, warn = [f"{la} goes back to seeing guest counts only, no names"], None
    return Prepared(f"{'Share' if on else 'Stop sharing'} your guest list for {_oh_name(oh)} with {la}", lines,
                    {"open_house_id": oh.id, "on": on}, warning=warn, confirm_label="Share it" if on else "Stop sharing")


async def _r_share_guests(db, user, p):
    await _run_route(user, "PUT", f"/api/open-houses/{p['open_house_id']}/share-guests", {"share_guests_with_listing_agent": p["on"]})
    return {"message": "Shared." if p["on"] else "Stopped sharing.", "link": f"/open-houses/visitors/{p['open_house_id']}"}

_simple("share_guest_list_with_listing_agent",
        "When the agent hosted someone else's listing: let that listing agent see the guests' names, emails and phones (on=true), or stop (on=false). "
        "Off unless the agent turns it on; nobody is emailed. open_house_id from search_open_houses.",
        _obj({"open_house_id": {"type": "string"}, "on": {"type": "boolean"}}, ["open_house_id", "on"]), _p_share_guests, _r_share_guests, MLS)


# ================================================================ BLOGS: publish / schedule / Agent Hub

def blog_flags(review: Optional[dict]) -> list[str]:
    """The draft's unresolved fact-check flags, in plain words (pure - unit tested)."""
    review = review or {}
    out = []
    for i in (review.get("open_issues") or [])[:5]:
        if not isinstance(i, dict):
            continue
        quote, problem = _s(i.get("quote"))[:120], _s(i.get("problem"))[:160]
        out.append(f"“{quote}” - {problem}" if quote and problem else (problem or f"“{quote}”"))
    more = len(review.get("open_issues") or []) - 5
    if more > 0:
        out.append(f"...and {more} more flag{'s' if more > 1 else ''}")
    nums = [_s(n)[:60] for n in (review.get("numbers_to_verify") or []) if _s(n)][:8]
    if nums:
        out.append("Numbers to double-check: " + ", ".join(nums))
    geo = review.get("geography_flags_open")
    if isinstance(geo, int) and geo > 0:
        out.append(f"{geo} town/school-district line{'s' if geo > 1 else ''} to check")
    if review.get("status") == "done" and review.get("checked") is False:
        out.append("The fact-check didn't finish on this draft - read it over first")
    return out


def _schedule_at(d: str, t: str) -> str:
    from zoneinfo import ZoneInfo
    at = datetime.fromisoformat(f"{d}T{t}:00").replace(tzinfo=ZoneInfo("America/New_York"))
    if at <= datetime.now(timezone.utc):
        raise ActionError("Pick a time that's still ahead.")
    return at.isoformat()


async def _p_blog_publish(db, user, a):
    blog = await _one(db, select(Blog).where(Blog.id == _need_id(a, "blog_id"), Blog.owner_id == user.id), "I couldn't find that post.")
    review = blog.ai_review or {}
    if review.get("status") == "running":
        raise ActionError("The AI writer is still working on that one - I'll tell you when the draft is ready.")
    if review.get("new") and review.get("status") in ("interview", "failed"):
        raise ActionError("That draft hasn't been written yet - open it on Blogs to finish it.")
    when = _s(a.get("when"))
    hub = a.get("show_on_agent_hub") if isinstance(a.get("show_on_agent_hub"), bool) else None
    if when not in ("now", "schedule", "") or (not when and hub is None):
        raise ActionError("Publish now, schedule it, or just show/hide it on your Agent Hub?")
    payload: dict = {"blog_id": blog.id}
    lines = []
    title = blog.title or "your post"
    if when == "now":
        if blog.status == "PUBLISHED":
            raise ActionError("It's already live.")
        payload["status"] = {"status": "PUBLISHED"}
        lines.append("Goes live now")
        head = f"Publish “{title[:80]}”"
    elif when == "schedule":
        if blog.status == "PUBLISHED":
            raise ActionError("It's already live - scheduling it would take it down until then.")
        d, t = _date_ok(a.get("date")), _time_ok(a.get("time") or "09:00")
        payload["status"] = {"status": "SCHEDULED", "scheduled_at": _schedule_at(d, t)}
        lines.append(f"Goes live {_nice_date(d)} at {_nice_time(t)} Eastern")
        head = f"Schedule “{title[:80]}”"
    else:
        head = f"{'Show' if hub else 'Hide'} “{title[:80]}” {'on' if hub else 'from'} your Agent Hub"
    if hub is not None and hub != bool(blog.agent_hub_visible):
        payload["hub"] = hub
        lines.append("Shows in your Agent Hub's Blogs section" if hub else "Hidden from your Agent Hub (the article's own link still works)")
    elif hub is not None and not when:
        raise ActionError(f"It's already {'on' if hub else 'off'} your Agent Hub.")
    elif when and blog.agent_hub_visible:
        lines.append("Shows in your Agent Hub's Blogs section")
    if not when and blog.status != "PUBLISHED" and hub:
        lines.append("It appears there once it's published")
    warn = None
    if when:
        flags = blog_flags(review)
        if flags:
            warn = "Check these before it goes live: " + " • ".join(flags)
        elif review.get("status") == "done" and not review.get("finished"):
            warn = "You haven't gone through Fact-check & finish on this draft yet - worth a read first."
    return Prepared(head, lines, payload, warning=warn, confirm_label={"now": "Publish", "schedule": "Schedule"}.get(when, "Confirm"))


async def _r_blog_publish(db, user, p):
    bid = p["blog_id"]
    if p.get("status"):
        await _run_route(user, "PATCH", f"/studio/blogs/{bid}/status", p["status"])
    if "hub" in p:
        await _run_route(user, "PATCH", f"/studio/blogs/{bid}/agent-hub-visibility", {"agent_hub_visible": p["hub"]})
    st = (p.get("status") or {}).get("status")
    return {"message": {"PUBLISHED": "It's live.", "SCHEDULED": "Scheduled."}.get(st, "Saved."), "link": f"/studio/blogs/{bid}"}

_simple("publish_blog_post",
        "Publish a blog draft now (when='now'), schedule it (when='schedule', date YYYY-MM-DD + time HH:MM Eastern), and/or show or hide it on the "
        "Agent Hub (show_on_agent_hub). blog_id from list_my_blogs. The card lists any fact-check flags first. Deleting a post stays on the Blogs page.",
        _obj({"blog_id": {"type": "string"}, "when": {"type": "string", "enum": ["now", "schedule"]}, "date": {"type": "string"},
              "time": {"type": "string"}, "show_on_agent_hub": {"type": "boolean"}}, ["blog_id"]), _p_blog_publish, _r_blog_publish, STUDIO)


# ================================================================ HELP TICKETS

async def _p_ticket_reply(db, user, a):
    t = await _one(db, select(SupportTicket).where(SupportTicket.id == _need_id(a, "ticket_id"), SupportTicket.agent_id == user.id),
                   "I couldn't find that Help ticket.")
    text = _s(a.get("text"))
    if not text:
        raise ActionError("What should the reply say?")
    lines = [f"“{text[:600]}”"] + (["Reopens the ticket"] if t.status == "closed" else [])
    return Prepared(f"Reply on your Help ticket “{(t.subject or '')[:80]}”", lines, {"ticket_id": t.id, "text": text[:5000]},
                    warning="This goes to a real person on the OpenHousePal team.", confirm_label="Send it")


async def _r_ticket_reply(db, user, p):
    await _run_route(user, "POST", f"/api/v1/support/tickets/{p['ticket_id']}/messages", {"body": p["text"]})
    return {"message": "Sent - you'll see their answer under Help.", "link": "/settings/help"}

_simple("reply_to_help_ticket",
        "Answer back on one of the agent's Help tickets (ticket_id from get_my_support_tickets; read the thread first with get_help_ticket). "
        "Replying to a closed ticket reopens it.",
        _obj({"ticket_id": {"type": "string"}, "text": {"type": "string"}}, ["ticket_id", "text"]), _p_ticket_reply, _r_ticket_reply)


# ================================================================ EMAIL SIGNATURE switches

FORM_BUTTONS = {"how_can_i_help": "Ready to Buy or Sell?", "home_value": "What's My Home Worth?", "showing": "Schedule a Showing",
                "buying": "I'm Buying", "selling": "I'm Selling", "make_me_sell": "Make Me Sell"}
SOCIALS = {"instagram": "Instagram", "facebook": "Facebook", "youtube": "YouTube", "tiktok": "TikTok"}
BANNER_PARTS = {"featured_listing": ("showFeaturedListing", "Featured listing"), "recent_sales": ("showRecentSales", "Recent sales"),
                "sales_stats": ("showStats", "Sales stats")}
MAX_BUTTONS = 2   # plus "My Agent Hub", which always leads (frontend MAX_CTA_BUTTONS)


def _match_button(buttons: list, key: str) -> Optional[dict]:
    k = key.strip().lower()
    for b in buttons:
        if k in (str(b.get("formType") or "").lower(), str(b.get("label") or "").lower()):
            return b
    return None


def apply_signature_changes(cfg: dict, changes: dict, my_form_types: Optional[set] = None, social_urls: Optional[set] = None) -> tuple[dict, list[str]]:
    """New signature config + card lines (pure - unit tested; raises ActionError). `changes`:
    banner (bool), banner_parts {featured_listing|recent_sales|sales_stats: bool},
    buttons {form type or label: bool}, socials {instagram|facebook|youtube|tiktok: bool}."""
    import copy
    new = copy.deepcopy(cfg or {})
    lines: list[str] = []
    if isinstance(changes.get("banner"), bool):
        new.setdefault("blockVisibility", {})["banner"] = changes["banner"]
        lines.append(f"Listings & sales banner: {_yn(changes['banner'])}")
    for part, on in (changes.get("banner_parts") or {}).items():
        if part not in BANNER_PARTS or not isinstance(on, bool):
            raise ActionError("The banner's parts are featured listing, recent sales and sales stats.")
        new.setdefault("banner", {})[BANNER_PARTS[part][0]] = on
        lines.append(f"Banner - {BANNER_PARTS[part][1]}: {_yn(on)}")
    buttons = [dict(b) for b in (new.get("ctaButtons") or []) if isinstance(b, dict)]
    for key, on in (changes.get("buttons") or {}).items():
        if not isinstance(on, bool):
            continue
        if str(key).strip().lower() in ("my agent hub", "agent hub", "agenthub"):
            raise ActionError("The My Agent Hub button is always on - it leads the signature.")
        b = _match_button(buttons, str(key))
        if b:
            b["enabled"] = on
            lines.append(f"Button “{b.get('label') or FORM_BUTTONS.get(b.get('formType'), 'Button')}”: {_yn(on)}")
            continue
        ft = next((f for f, label in FORM_BUTTONS.items() if str(key).strip().lower() in (f, label.lower())), None)
        if not on:
            raise ActionError(f"There's no “{key}” button on your signature.")
        if not ft:
            raise ActionError("Signature buttons open one of your forms: " + ", ".join(FORM_BUTTONS.values()) + ".")
        if my_form_types is not None and ft not in my_form_types:
            raise ActionError(f"You don't have the {FORM_BUTTONS[ft]} form yet - set it up in Forms & Invites first.")
        if len(buttons) >= MAX_BUTTONS:
            raise ActionError(f"Your signature already has {MAX_BUTTONS} buttons besides My Agent Hub - turn one off on the Signature page to swap it.")
        buttons.append({"id": f"cta-{ft}", "label": FORM_BUTTONS[ft], "formType": ft, "url": "", "enabled": True})
        lines.append(f"Add a “{FORM_BUTTONS[ft]}” button")
    if changes.get("buttons"):
        new["ctaButtons"] = buttons
    for net, on in (changes.get("socials") or {}).items():
        if net not in SOCIALS or not isinstance(on, bool):
            raise ActionError("Socials on the signature: Instagram, Facebook, YouTube, TikTok.")
        if on and social_urls is not None and net not in social_urls:
            raise ActionError(f"Add your {SOCIALS[net]} link first (Settings → Your socials), then I can show it.")
        new.setdefault("fields", {}).setdefault(net, {})["include"] = on
        lines.append(f"{SOCIALS[net]} icon: {_yn(on)}")
    if not lines:
        raise ActionError("What should change on your signature - the buttons, the listings banner, or your socials?")
    return new, lines


async def _sig_context(db, user, need_forms: bool):
    from app.models.database import AgentHub
    data = await _run_route(user, "GET", "/email-signature/me")
    hub = (await db.execute(select(AgentHub.config).where(AgentHub.owner_id == user.id))).scalar_one_or_none() or {}
    socials = {l.get("id") for l in (hub.get("socialLinks") or []) if isinstance(l, dict) and l.get("enabled") and _s(l.get("url"))}
    forms = None
    if need_forms:
        rows = await _run_route(user, "GET", "/agent-hub/forms/me")
        forms = {r.get("form_type") for r in rows or [] if isinstance(r, dict)}
    return (data or {}).get("config") or {}, forms, socials


def _sig_changes(a: dict) -> dict:
    return {k: a[k] for k in ("banner", "banner_parts", "buttons", "socials") if a.get(k) not in (None, {}, "")}


async def _p_signature(db, user, a):
    changes = _sig_changes(a)
    cfg, forms, socials = await _sig_context(db, user, any(v is True for v in (changes.get("buttons") or {}).values()))
    _, lines = apply_signature_changes(cfg, changes, forms, socials)
    warn = "If you have the Signature page open in another tab, refresh it first so it doesn't save over this."
    try:
        info = await _run_route(user, "GET", "/email-signature/me/html")
        if (info or {}).get("installed"):
            warn = "Your signature is already in your email app - after this, copy it again from the Signature page so the change shows there."
    except ActionError:
        pass
    return Prepared("Change your email signature", lines, {"changes": changes}, warning=warn)


async def _r_signature(db, user, p):
    # Re-read the signature now (not the copy from when the card was made) and apply just these switches.
    changes = p.get("changes") or {}
    cfg, forms, socials = await _sig_context(db, user, any(v is True for v in (changes.get("buttons") or {}).values()))
    new, _ = apply_signature_changes(cfg, changes, forms, socials)
    await _run_route(user, "PUT", "/email-signature/me", {"config": new})
    return {"message": "Signature saved.", "link": "/studio/email-signature"}

_simple("update_email_signature",
        "Switch parts of the agent's email signature on or off: banner (the listings & sales banner), banner_parts {featured_listing, recent_sales, sales_stats: true/false}, "
        "buttons {form type or button label: true/false} - turning on one they don't have adds it (max 2 besides My Agent Hub; forms: "
        + ", ".join(f"{k} ({v})" for k, v in FORM_BUTTONS.items()) + "), socials {instagram, facebook, youtube, tiktok: true/false}. "
        "Colors, fonts and the look stay on the Signature page. Read get_email_signature first.",
        _obj({"banner": {"type": "boolean"}, "banner_parts": {"type": "object", "additionalProperties": {"type": "boolean"}},
              "buttons": {"type": "object", "additionalProperties": {"type": "boolean"}},
              "socials": {"type": "object", "additionalProperties": {"type": "boolean"}}}), _p_signature, _r_signature, SIGNATURE)


# ================================================================ CALENDAR + CRM

CAL = {"google": "Google Calendar", "microsoft": "Outlook Calendar"}


async def _p_resync(db, user, a):
    data = await _run_route(user, "GET", "/integrations") or {}
    on = [p for p in CAL if isinstance(data.get(f"{p}_calendar"), dict) and data[f"{p}_calendar"].get("connected")]
    if not on:
        raise ActionError("Your calendar isn't connected - connect it on Tools → Calendar (it needs your own Google sign-in).")
    if any(data[f"{p}_calendar"].get("needs_reconnect") for p in on):
        raise ActionError("Your calendar's sign-in expired - tap Reconnect on Tools → Calendar first.")
    return Prepared(f"Sync your {' and '.join(CAL[p] for p in on)} again",
                    ["Sends every open follow-up with a date and your upcoming open houses to the OpenHousePal calendar again",
                     "Doesn't touch your other calendars"], {"providers": on}, confirm_label="Sync now")


async def _r_resync(db, user, p):
    for prov in p.get("providers") or []:
        if prov in CAL:
            await _run_route(user, "POST", f"/integrations/{prov}/resync")
    return {"message": "Syncing now - it shows on your calendar in a minute or two.", "link": "/studio/calendar"}

_simple("resync_calendar", "Sync the agent's connected calendar again (Tools → Calendar's Sync now) - for 'my follow-ups aren't on my calendar'. Connecting a calendar stays on the page (their sign-in).",
        _obj({}), _p_resync, _r_resync)


async def _p_crm_test(db, user, a):
    data = await _run_route(user, "GET", "/integrations") or {}
    where = ([("Follow Up Boss")] if data.get("crm_fub_connected") else []) + (["your webhook"] if data.get("crm_webhook_url") else []) \
        + (["your CRM's lead email"] if data.get("crm_parse_email") else [])
    if not where:
        raise ActionError("Your CRM isn't set up yet - add your Follow Up Boss key, webhook or CRM email on Tools → CRM first.")
    lines = [f"Goes to: {', '.join(where)}", "A made-up “Test Lead” - not a real client"]
    if not data.get("crm_push_enabled"):
        lines.append("Sending real leads is switched off - the test still goes")
    return Prepared("Send a test lead to your CRM", lines, {}, warning="This adds a test contact in your CRM - delete it there after.", confirm_label="Send test")


async def _r_crm_test(db, user, p):
    out = await _run_route(user, "POST", "/integrations/crm/test") or {}
    if not any(out.get(k) == "ok" for k in ("webhook", "email", "fub")):
        # Nothing got through - show it as failed, not a green "done" (11/02 review)
        raise ActionError(out.get("detail") or "The test lead didn't get through - check Tools → CRM.")
    return {"message": out.get("detail") or "Sent.", "link": "/studio/integrations"}

_simple("send_crm_test_lead", "Send a test lead to the agent's CRM (Follow Up Boss / webhook / CRM email) to check it's hooked up. Keys and settings stay on Tools → CRM.",
        _obj({}), _p_crm_test, _r_crm_test)


# ================================================================ ACCOUNT SETUP steps

SETUP_STEPS = {"about": "About you", "contact": "How to reach you", "license": "License number", "photo": "Your photo", "story": "Your story",
               "socials": "Your socials", "sales": "Your sales", "reviews": "Reviews", "brand": "Your brand", "hub": "Agent Hub", "forms": "Your forms",
               "signature": "Email signature", "openhouse": "Open house defaults", "alerts": "Alerts & follow-ups", "calendar": "Calendar",
               "mailbox": "Your email", "crm": "Your CRM", "areas": "Towns you serve", "homes": "Website homes", "website": "Your website", "domain": "Web address"}
NOT_USING = {"calendar": "I don't need a calendar", "mailbox": "Keep sending from OpenHousePal", "crm": "I don't use a CRM"}
REQUIRED = {"about", "contact", "license"}


def setup_change(step: str, choice: str, status: Optional[str]) -> tuple[dict, str, list[str]]:
    """(PUT /setup/state body, card title, lines) - pure, unit tested. status = the step's current status."""
    name = SETUP_STEPS.get(step)
    if not name:
        raise ActionError("Which setup step?")
    if status is None:
        raise ActionError(f"“{name}” isn't part of setup on your plan.")
    if choice == "skip":
        if step in REQUIRED:
            raise ActionError(f"“{name}” is required - it can't be skipped.")
        if status == "done":
            raise ActionError(f"“{name}” is already done.")
        if status in ("skipped", "not_using"):
            raise ActionError(f"“{name}” is already {'skipped' if status == 'skipped' else 'marked not using'}.")
        return {"skip": step}, f"Skip “{name}” in setup", ["Stays on your dashboard as skipped - come back to it any time"]
    if choice == "not_using":
        if step not in NOT_USING:
            raise ActionError("“Not using” is only for Calendar, Your email and Your CRM - other steps can be skipped.")
        if status == "not_using":
            raise ActionError(f"“{name}” is already marked not using.")
        if status == "done":
            raise ActionError(f"“{name}” is already set up.")
        return {"not_using": step}, f"Mark “{name}” as not using", [f"“{NOT_USING[step]}”", "Counts as finished and takes it off your dashboard",
                                                                  "Connecting later still works"]
    if choice == "bring_back":
        if status == "not_using":
            return {"use_again": step}, f"Bring back “{name}” in setup", ["Shows up again as a step to do"]
        if status == "skipped":
            return {"unskip": step}, f"Bring back “{name}” in setup", ["Shows up again as a step to do"]
        raise ActionError(f"“{name}” isn't skipped.")
    raise ActionError("Skip it, mark it not using, or bring it back?")


async def _p_setup(db, user, a):
    step = _s(a.get("step"))
    data = await _run_route(user, "GET", "/setup") or {}
    status = next((s.get("status") for s in data.get("steps") or [] if isinstance(s, dict) and s.get("id") == step), None)
    body, title, lines = setup_change(step, _s(a.get("choice")), status)
    return Prepared(title, lines, {"body": body})


async def _r_setup(db, user, p):
    await _run_route(user, "PUT", "/setup/state", p["body"])
    return {"message": "Saved.", "link": "/setup"}

_simple("set_setup_step",
        "Account setup: skip an optional step, mark Calendar / Your email (mailbox) / Your CRM as not using (counts as finished), or bring a skipped/not-using "
        "step back. step ids: " + ", ".join(f"{k} ({v})" for k, v in SETUP_STEPS.items()) + ". Check get_setup_progress first.",
        _obj({"step": {"type": "string", "enum": list(SETUP_STEPS)}, "choice": {"type": "string", "enum": ["skip", "not_using", "bring_back"]}}, ["step", "choice"]),
        _p_setup, _r_setup)
