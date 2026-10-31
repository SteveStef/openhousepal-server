"""More read-only lookups for the AI helper (10/30) - each one is the GET the app's own page makes,
called as the agent through ai_route_bridge (so the same plan and ownership checks apply), then
trimmed. Paths are fixed here; the model only fills in ids / filters, which are checked to be
plain ids before they go into a path.

Never here: anything that calls Bright MLS live (e.g. GET /agent-hub/me/recent-sales), PayPal
(GET /subscriptions/plan-terms), or Google; and no routes that change data on read.
"""
import re
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import User
from app.services.ai.ai_route_bridge import compact, lookup

_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

MLS = {"BASIC", "PREMIUM", "IDX"}
SHOWCASE = {"PREMIUM", "IDX"}
IDX = {"IDX"}
REVIEWS = {"PREMIUM", "IDX", "PRO_NO_MLS"}


def _id(args: dict, key: str) -> str:
    v = str(args.get(key) or "")
    if not _ID.match(v):
        raise ValueError(f"{key} is missing")
    return v


def _obj(props: Optional[dict] = None, required: Optional[list] = None) -> dict:
    return {"type": "object", "properties": props or {}, **({"required": required} if required else {})}


# name -> (description, input_schema, plans or None, async fn(db, user, args) -> result)
LOOKUPS: Dict[str, tuple] = {}


def _route(name: str, description: str, path: str, *, schema: Optional[dict] = None, plans=None,
           ids=(), query=lambda a: None, **compact_kw):
    async def run(db: AsyncSession, user: User, args: dict) -> Any:
        p = path.format(**{k: _id(args, k) for k in ids})
        ok, data = await lookup(user, p, params=query(args), **compact_kw)
        return data
    LOOKUPS[name] = (description, schema or _obj(), plans, run)


_q = lambda **keys: (lambda a: {k: a[src] for k, src in keys.items() if a.get(src) not in (None, "")} or None)

# --- Follow-ups / leads / client emails ---
_route("list_followups", "List the agent's follow-ups (to-dos) with their ids, due times and who they're for. Filter by bucket (overdue, today, upcoming, someday) and/or one lead. Use before changing or checking off a follow-up.",
       "/tasks", schema=_obj({"bucket": {"type": "string", "enum": ["overdue", "today", "upcoming", "someday"]}, "lead_id": {"type": "string"}, "include_done": {"type": "boolean"}}),
       query=lambda a: {k: v for k, v in {"bucket": a.get("bucket"), "lead_id": a.get("lead_id"), "include_done": "true" if a.get("include_done") else None}.items() if v} or None, max_list=25)
_route("get_followup_settings", "The agent's automatic follow-up settings: the master switch, each rule (on/off, when, style) and calendar alerts.", "/tasks/auto-rules", max_str=300)
_route("get_lead_notes", "Every note on one lead, newest first, with note ids.", "/leads/{lead_id}/notes", schema=_obj({"lead_id": {"type": "string"}}, ["lead_id"]), ids=("lead_id",))
_route("get_email_tracker_people", "Email Tracker: each client the agent emails automatically, whether those emails are on, weekly or off, and why (unopened, unsubscribed, bounced).", "/client-emails/people", max_list=40)
_route("get_client_email_log", "Emails OpenHousePal sent the agent's clients, with opened/clicked, newest first. Filter by one client's email address.",
       "/client-emails", schema=_obj({"email": {"type": "string"}}), query=_q(email="email"), max_list=20)

# --- Showcases ---
_route("list_showcases", "All the agent's Showcases with ids, client, status (active or asleep and why), email mode, star, and new-match / activity counts.", "/collections/", plans=SHOWCASE, max_list=40)
_route("get_showcase_inbox", "Across all Showcases: tour requests and recent likes, comments and search-change requests from clients.", "/collections/inbox", plans=SHOWCASE, max_list=25)
_route("get_showcase_tours", "Tour requests for one Showcase, with tour ids and status.", "/collections/{collection_id}/tours", schema=_obj({"collection_id": {"type": "string"}}, ["collection_id"]), ids=("collection_id",), plans=SHOWCASE)

# --- Open houses ---
_route("get_open_house_details", "One open house kit: its switches (Guest Showcases, Similar Homes, Guest Pass), private note, sign-in look, date, and what this plan can turn on.", "/api/open-houses/{open_house_id}", schema=_obj({"open_house_id": {"type": "string"}}, ["open_house_id"]), ids=("open_house_id",))
_route("get_guest_requests", "Disclosure and second-visit requests guests made at the agent's open houses, with request ids and status.", "/api/open-houses/form-requests", max_list=25)
_route("get_host_requests", "Requests from other agents to host the agent's listings (Opens -> Manager), with request ids, dates and status.", "/api/open-house-manager/host-requests",
       schema=_obj({"status": {"type": "string", "enum": ["pending", "approved", "declined", "countered"]}}), query=_q(status="status"), plans=MLS, max_list=25)
_route("get_my_hosting_requests", "The agent's own requests to host other agents' listings and their status (pending, approved, countered...).", "/api/open-house-manager/my-requests", plans=MLS, max_list=25)
_route("get_my_listings_for_hosting", "The agent's own listings in Opens -> Manager: which need a host, who is hosting when, host notes, with listing keys.", "/api/open-house-manager/my-listings", plans=MLS, max_list=25)
_route("get_team_host_asks", "Asks from the agent's team for someone to host an open house, with ids.", "/api/open-house-manager/team/broadcasts/incoming", plans=MLS, max_list=20)

# --- Account / settings / site ---
_route("get_ai_usage", "How much of this month's AI allowance the agent has used (percent).", "/ai/usage/monthly")
_route("get_email_settings", "Which notification emails the agent gets, their daily briefing time, the Showcase invite delay, client email rules, and whether Gmail is connected.", "/api/v1/email-settings", max_str=200)
_route("get_my_towns", "The towns/areas the agent serves (used for forms, buyer matching, blog cards and IDX town pages), in order, with whether each town's info is ready.", "/studio/sites/me/areas", query=lambda a: {"lite": "true"}, max_list=15)
_route("get_my_reviews", "The agent's client reviews: average rating, counts, recent reviews with ids and whether each shows, and review settings (Google/Zillow links, show on hub/website, minimum stars).", "/agent-hub/reviews", plans=REVIEWS, max_list=15)
_route("get_my_support_tickets", "The agent's Help tickets to the OpenHousePal team, with status and whether support replied.", "/api/v1/support/tickets", max_list=15)
_route("get_setup_progress", "Account setup checklist: which steps are done, skipped, not using, or still to do.", "/setup", max_str=200)


# --- Tools (Studio) ---
STUDIO = {"PREMIUM", "IDX", "PRO_NO_MLS"}
_route("list_my_spotlights", "The agent's Listing Spotlights (single-listing websites) with ids, address, Draft/Published/Offline status and public link.", "/studio/property-sites", plans=IDX, max_list=25)
_route("get_spotlight_stats", "Visitors, views and leads for the agent's Listing Spotlights (totals and per Spotlight).", "/studio/property-sites/analytics", plans=IDX)
_route("list_my_designs", "The agent's Studio designs (flyers, social posts, postcards...) newest first, with type, situation and status.", "/studio/designs/me", plans=STUDIO, max_list=20)
_route("list_my_forms", "The agent's forms (Forms & Invites) with ids and response counts.", "/agent-hub/forms/me", max_list=25)
_route("get_form_stats", "Form totals: views, responses, unread responses.", "/agent-hub/forms/stats/summary")
_route("list_my_invites", "The agent's event invites (open house, broker's open, client events) with RSVP counts.", "/invites", max_list=20)
_route("list_my_blogs", "The agent's blog posts with status (draft, scheduled, published).", "/studio/blogs/me", plans=STUDIO, max_list=20)
_route("get_branding", "The agent's Branding (brand kit): colors, fonts, Classic/Luxe look, logos on file.", "/studio/brand-kit")


# --- Deep detail on one feature (10/30) - every option, every plan. One file per feature in
# helper_depth/, read on demand so the always-sent guide stays small.
_DEPTH = Path(__file__).with_name("helper_depth")
DEPTH_KEYS = sorted(p.stem for p in _DEPTH.glob("*.md")) if _DEPTH.exists() else []


async def _feature_details(db: AsyncSession, user: User, args: dict) -> Any:
    key = str(args.get("feature") or "")
    if key not in DEPTH_KEYS:
        return {"error": "unknown_feature", "features": DEPTH_KEYS}
    return {"feature": key, "details": (_DEPTH / f"{key}.md").read_text(encoding="utf-8")}

if DEPTH_KEYS:
    LOOKUPS["get_feature_details"] = ("Full detail on one OpenHousePal feature - every option, setting, limit and which plans get it. Use for detailed how-to or 'what are my options' questions.",
                                      _obj({"feature": {"type": "string", "enum": DEPTH_KEYS}}, ["feature"]), None, _feature_details)


async def _my_account(db: AsyncSession, user: User, args: dict) -> dict:
    return {
        "name": f"{user.first_name} {user.last_name}".strip(),
        "plan": user.plan_tier, "subscription_status": user.subscription_status,
        "trial_ends_at": getattr(user, "trial_ends_at", None), "next_billing_date": getattr(user, "next_billing_date", None),
        "broker_authorized": bool(getattr(user, "broker_authorized", False)),
    }
LOOKUPS["get_my_account"] = ("The agent's plan, subscription status, trial end and next billing date, and whether their broker has approved them. Plan names/prices are in the guide.", _obj(), None, _my_account)


async def _past_sales(db: AsyncSession, user: User, args: dict) -> Any:
    """Their sold homes as their Agent Hub shows them - the local MLS copy + ones they added by hand.
    Never the live-Bright route."""
    from app.models.database import AgentHub
    from app.services.agent_hub_live import live_hub_config
    hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == user.id))).scalar_one_or_none()
    cfg = await live_hub_config(db, user, (hub.config if hub else {}) or {})
    sales = [s for s in (cfg.get("recentSales") or []) if isinstance(s, dict)]
    return {"count": len(sales), "sales": compact(sales, max_list=25, max_str=200)}
LOOKUPS["get_my_past_sales"] = ("The agent's own past sales (sold homes) - address, price, date, side - from OpenHousePal's copy of the MLS plus any they added by hand.", _obj(), None, _past_sales)


async def _my_site(db: AsyncSession, user: User, args: dict) -> dict:
    from app.models.database import Site
    from app.services.website import site_service
    site = (await db.execute(select(Site).where(Site.owner_id == user.id))).scalar_one_or_none()
    if not site:
        return {"has_site": False}
    return {"has_site": True, "status": site.status, "is_enabled": site.is_enabled,
            "published_at": site.published_at, "address": f"https://{site_service.default_hostname(site)}"}
LOOKUPS["get_my_website"] = ("The agent's IDX website: whether it's published and turned on, and its address.", _obj(), IDX, _my_site)


def definitions_for(user: User) -> list:
    return [{"name": n, "description": d, "input_schema": s} for n, (d, s, plans, _) in LOOKUPS.items() if _ok(user, plans)]


def _ok(user: User, plans) -> bool:
    return plans is None or user.is_admin or user.plan_tier in plans


async def run_lookup(db: AsyncSession, user: User, name: str, args: dict) -> Any:
    d, s, plans, fn = LOOKUPS[name]
    if not _ok(user, plans):
        return {"error": "not_on_plan", "message": "That isn't part of this agent's plan."}
    try:
        return await fn(db, user, args or {})
    except ValueError as e:
        return {"error": "bad_input", "message": str(e)}
