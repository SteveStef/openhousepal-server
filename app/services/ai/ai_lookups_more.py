"""ai_lookups_more - filled in 11/01 (AI helper additions from the capability review). Loaded at the end of ai_lookups.py.

Read-only, like the rest of ai_lookups: each one is the GET the page itself makes, called as the
agent (same plan / ownership checks), trimmed to what the model needs. Two exceptions read the
agent's own rows straight from the database because the page's GET also marks things read
(a Help ticket's thread) - a lookup never changes anything.
"""
from datetime import date as _date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.database import SupportTicket, User
from app.services.ai.ai_lookups import LOOKUPS, MLS, _id, _obj
from app.services.ai.ai_route_bridge import RouteError, call_as_agent

STUDIO = {"PREMIUM", "IDX", "PRO_NO_MLS"}        # Blogs (require_studio_design_access)
AGENT_HUB = {"PREMIUM", "IDX", "PRO_NO_MLS"}     # Agent Hub (require_agent_hub_access)
SIGNATURE = {"IDX", "PRO_NO_MLS"}                # Email signature (require_email_signature_access)


async def _get(user: User, path: str, params: Optional[dict] = None) -> tuple[bool, Any]:
    try:
        return True, await call_as_agent(user, "GET", path, params=params)
    except RouteError as e:
        return False, {"error": "lookup_failed", "message": str(e)}


def _t(v: Any) -> Optional[str]:
    return str(v)[:5] if v else None


def _day(v: Any) -> Optional[str]:
    return str(v)[:10] if v else None


# ================================================================ DISCOVERY: homes to host

def _norm(s: Any) -> str:
    return " ".join(str(s or "").lower().replace(",", " ").split())


def discovery_rows(listings: list, statuses: dict, my_requests: list, town: str = "", day: Optional[str] = None, limit: int = 10, me: Optional[str] = None) -> dict:
    """Shapes the Discovery page's own listings for the model (pure - unit tested). `town` keeps
    only homes in that town (city, or the street line); `day` (YYYY-MM-DD) says, per home, whether
    that day is open to host."""
    want = _norm(town)
    # their own listings are hosted from My Opens, not asked for here
    rows = [l for l in listings if isinstance(l, dict) and not (me and l.get("OnPlatformAgentId") == me)]
    if want:
        rows = [l for l in rows if want in _norm(l.get("City")) or want in _norm(l.get("Street"))]
    mine_by_key: dict = {}
    hosting_that_day = None
    for r in my_requests or []:
        if not isinstance(r, dict):
            continue
        mine_by_key.setdefault(r.get("listing_key"), []).append(r)
        if day and r.get("status") == "approved" and _day(r.get("requested_date")) == day:
            hosting_that_day = r.get("property_address") or "another open house"
    out = []
    for l in rows[:max(1, min(limit, 25))]:
        key = l.get("ListingKey")
        st = statuses.get(key) or {}
        opens = [{"date": _day(o.get("scheduled_date")), "start": _t(o.get("scheduled_start_time")), "end": _t(o.get("scheduled_end_time"))}
                 for o in (st.get("scheduled_opens") or []) if isinstance(o, dict)][:4]
        row = {
            "listing_key": key,
            "address": ", ".join(x for x in (l.get("Street"), l.get("City")) if x),
            "price": l.get("Price"),
            "beds": l.get("Beds"), "baths": l.get("Baths"),
            "listing_agent": l.get("ListAgentFullName"), "listing_office": l.get("ListOfficeName"),
            "listing_agent_on_openhousepal": bool(l.get("AgentOnPlatform")),
            "can_ask_in_app": bool(l.get("AgentOnPlatform")) and bool(l.get("HostRequestEligible", True)),
            "miles_away": round(l["DistanceFromLandmark"], 1) if isinstance(l.get("DistanceFromLandmark"), (int, float)) else None,
            "new_listing": bool(l.get("New")),
            "days_on_market": l.get("DaysOnMarket"),
            "open_houses_already_set": opens,
        }
        if l.get("HostingBlocked") == "coming_soon":
            row["hosting_note"] = "Coming soon - no open houses until it goes active"
        elif l.get("HostsFrom"):
            row["hosting_note"] = f"Can be hosted from {l['HostsFrom']}"
        mine = mine_by_key.get(key) or []
        if mine:
            row["your_requests"] = [{"request_id": r.get("id"), "date": _day(r.get("requested_date")), "status": r.get("status")} for r in mine][:3]
        if day:
            taken = any(o["date"] == day for o in opens)
            blocked = l.get("HostingBlocked") == "coming_soon" or (l.get("HostsFrom") and str(l["HostsFrom"]) > day)
            row["on_that_day"] = ("can't host yet" if blocked else "already has an open house" if taken else "open to host")
        out.append(row)
    res: dict = {"homes": out, "count_in_search": len(rows)}
    if hosting_that_day:
        res["you_already_host_that_day"] = f"You're already confirmed to host {hosting_that_day} that day - it's one open house per day."
    return res


async def _homes_to_host(db: AsyncSession, user: User, args: dict) -> Any:
    ok, data = await _get(user, "/api/discovery")
    if not ok:
        return data
    prefs = (data or {}).get("preferences")
    listings = [l for l in ((data or {}).get("data") or []) if isinstance(l, dict)]
    # Newest listings first (owner, 11/01: "based on the filter the top newest listings").
    listings.sort(key=lambda l: (not isinstance(l.get("DaysOnMarket"), (int, float)), l.get("DaysOnMarket") or 0))
    if not prefs or not (prefs.get("landmark_address") or prefs.get("cities") or prefs.get("townships") or prefs.get("school_districts")):
        return {"homes": [], "note": "Their Discovery search area isn't set yet. Tell them so in one line and set it up right away with set_discovery_search (its card offers their listings and towns as the center, miles, price and beds - it saves to their Discovery page); say they can tap one or tell you another town. Once it is saved, look again."}
    day = None
    if args.get("date"):
        try:
            day = _date.fromisoformat(str(args["date"])[:10]).isoformat()
        except ValueError:
            raise ValueError("date must be YYYY-MM-DD")
    town = str(args.get("town") or "").strip()
    want = _norm(town)
    keys = [l.get("ListingKey") for l in listings if isinstance(l, dict) and l.get("OnPlatformAgentId") != user.id and (not want or want in _norm(l.get("City")) or want in _norm(l.get("Street")))][:25]
    statuses: dict = {}
    if keys:
        ok_s, st = await _get(user, "/api/open-house-manager/listing-status", {"listing_keys": ",".join(k for k in keys if k)})
        statuses = st if ok_s and isinstance(st, dict) else {}
    ok_m, mine = await _get(user, "/api/open-house-manager/my-requests")
    res = discovery_rows(listings, statuses, mine if ok_m and isinstance(mine, list) else [], town, day, int(args.get("limit") or 10), user.id)
    area = ", ".join(x for x in [
        f"{prefs.get('miles'):g} miles around {prefs.get('landmark_address')}" if prefs.get("landmark_address") and prefs.get("miles") else None,
        ("towns: " + ", ".join(prefs.get("cities") or [])) if prefs.get("cities") else None,
        # the price range they set (owner, 11/01: max price next to the min)
        (f"${prefs['min_price']:,.0f}-${prefs['max_price']:,.0f}" if prefs.get("min_price") and prefs.get("max_price")
         else f"${prefs['min_price']:,.0f}+" if prefs.get("min_price") else f"up to ${prefs['max_price']:,.0f}" if prefs.get("max_price") else None),
        f"{prefs['min_bedrooms']}+ beds" if prefs.get("min_bedrooms") else None,
    ] if x) or "their saved Discovery filters"
    res["search_area"] = area
    # Every matching home, newest first, for the Discovery cards under the reply (the model sees `limit`).
    res["card_keys"] = [l.get("ListingKey") for l in listings if l.get("ListingKey") and l.get("OnPlatformAgentId") != user.id
                        and (not want or want in _norm(l.get("City")) or want in _norm(l.get("Street")))][:40]
    if want and not res["homes"]:
        res["note"] = (f"None of the homes in their Discovery search ({area}) are in {town}. They can widen the search on Discovery - "
                       "changing it there changes it for the page too.")
    res["how_to_ask"] = ("To ask to host one: request_to_host with its listing_key, a date and times (only when can_ask_in_app). "
                         "If the listing agent isn't on OpenHousePal, Discovery has a text/email they send themselves.")
    return res

LOOKUPS["find_homes_to_host"] = (
    "Homes the agent could host an open house at - the Discovery page's own list (their saved search area, closest first) with "
    "address, price, listing agent, miles away, open houses already set, and their own requests. Optional town (keeps only that town) "
    "and date YYYY-MM-DD (says per home if that day is open to host). Use for 'what can I host near Malvern this Saturday?' or "
    "'any homes that need a host this weekend?'.",
    _obj({"town": {"type": "string"}, "date": {"type": "string"}, "limit": {"type": "integer"}}), MLS, _homes_to_host)


# ================================================================ BLOG + AGENT HUB STATS

async def _blog_stats(db: AsyncSession, user: User, args: dict) -> Any:
    if args.get("blog_id"):
        bid = _id(args, "blog_id")
        ok, blog = await _get(user, f"/studio/blogs/{bid}")
        if not ok:
            return blog
        ok, a = await _get(user, f"/studio/blogs/{bid}/analytics")
        if not ok:
            return a
        return {"title": blog.get("title"), "status": blog.get("status"), "published_at": _day(blog.get("published_at")),
                "views": a.get("views"), "lead_form_sent": a.get("lead_submissions"), "contact_taps": a.get("contact_clicks"),
                "home_card_taps": a.get("property_clicks"), "link_taps": a.get("link_clicks"), "video_plays": a.get("video_plays"),
                "qr_scans": a.get("qr_scans"), "note": "Counts are all time. Views = times the article page was opened (not unique people)."}
    ok, rows = await _get(user, "/studio/blogs/me")
    if not ok:
        return rows
    rows = [r for r in rows or [] if isinstance(r, dict)]
    rows.sort(key=lambda r: (r.get("status") != "PUBLISHED", -(r.get("view_count") or 0)))
    return {"posts": [{"blog_id": r.get("id"), "title": r.get("title"), "status": r.get("status"), "views": r.get("view_count") or 0,
                       "published_at": _day(r.get("published_at")), "on_agent_hub": r.get("agent_hub_visible")} for r in rows[:20]],
            "total_views": sum(r.get("view_count") or 0 for r in rows),
            "note": "Views are all time (times the article was opened, not unique people). Pass blog_id for one post's taps, leads and QR scans."}

LOOKUPS["get_blog_stats"] = (
    "How many times the agent's blog posts were read: views per post (all time), or for one post (blog_id) also lead forms sent, "
    "contact taps, home taps, video plays and QR scans. Use for 'how many people read my post?'.",
    _obj({"blog_id": {"type": "string"}}), STUDIO, _blog_stats)


def hub_counts(events: list, now: Optional[datetime] = None) -> dict:
    """Agent Hub events (visit / qr / form, with timestamps) -> counts for 7, 30 and 60 days."""
    now = now or datetime.now(timezone.utc)
    out = {f"last_{d}_days": {"visits": 0, "qr_scans": 0, "form_responses": 0} for d in (7, 30, 60)}
    names = {"visit": "visits", "qr": "qr_scans", "form": "form_responses"}
    for e in events or []:
        try:
            at = datetime.fromisoformat(str(e.get("timestamp")).replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        k = names.get(e.get("type"))
        for d in (7, 30, 60):
            if k and now - at <= timedelta(days=d):
                out[f"last_{d}_days"][k] += 1
    return out


async def _hub_stats(db: AsyncSession, user: User, args: dict) -> Any:
    ok, events = await _get(user, "/agent-hub/me/analytics")
    if not ok:
        return events
    return {**hub_counts(events if isinstance(events, list) else []),
            "note": "visits = Agent Hub page visits, qr_scans = scans of their Hub QR code, form_responses = answers to any of their forms. Only the last 60 days are kept here."}

LOOKUPS["get_agent_hub_stats"] = (
    "Agent Hub traffic: page visits, QR code scans and form responses for the last 7, 30 and 60 days. Use for 'how many people visited my Agent Hub / page?'.",
    _obj(), AGENT_HUB, _hub_stats)


# ================================================================ HELP TICKET THREAD

async def _ticket(db: AsyncSession, user: User, args: dict) -> Any:
    tid = _id(args, "ticket_id")
    t = (await db.execute(select(SupportTicket).options(selectinload(SupportTicket.messages))
                          .where(SupportTicket.id == tid, SupportTicket.agent_id == user.id))).scalar_one_or_none()
    if not t:
        return {"error": "not_found", "message": "No Help ticket with that id."}
    msgs = list(t.messages or [])
    return {"ticket_id": t.id, "subject": t.subject, "status": t.status, "kind": t.category or "support",
            "opened": _day(t.created_at),
            "messages": [{"from": "OpenHousePal team" if m.sender_type == "admin" else "you", "at": _day(m.created_at),
                          "text": (m.body or "")[:1500], **({"screenshots": len(m.attachments)} if m.attachments else {})} for m in msgs[-12:]],
            **({"earlier_messages_not_shown": len(msgs) - 12} if len(msgs) > 12 else {}),
            "note": "Tell them what the team said in plain words. They can answer back with reply_to_help_ticket."}

LOOKUPS["get_help_ticket"] = (
    "One Help ticket's full conversation - what the agent wrote and the OpenHousePal team's actual replies. ticket_id from "
    "get_my_support_tickets. Use for 'what did support say?' / 'did they answer my ticket?'.",
    _obj({"ticket_id": {"type": "string"}}, ["ticket_id"]), None, _ticket)


# ================================================================ EMAIL SIGNATURE

FORM_BUTTONS = {"how_can_i_help": "Ready to Buy or Sell?", "home_value": "What's My Home Worth?", "showing": "Schedule a Showing",
                "buying": "I'm Buying", "selling": "I'm Selling", "make_me_sell": "Make Me Sell"}
SOCIALS = ("instagram", "facebook", "youtube", "tiktok")
SIG_FIELDS = {"headshot": "Photo", "name": "Name", "title": "Title", "company": "Brokerage", "team": "Team", "phone": "Phone",
              "email": "Email", "website": "Website", "agentHubUrl": "Agent Hub link"}
INSTALL_APPS = {"gmail": "Gmail", "outlook": "Outlook (web / new Outlook)", "outlook_desktop": "Outlook for Windows", "apple": "Apple Mail"}


def signature_summary(cfg: dict, html_info: Optional[dict] = None) -> dict:
    """The saved signature (GET /email-signature/me config) in plain terms (pure - unit tested)."""
    cfg = cfg or {}
    fields = cfg.get("fields") or {}
    banner = cfg.get("banner") or {}
    on_banner = bool((cfg.get("blockVisibility") or {}).get("banner", True))
    buttons = [{"label": b.get("label") or FORM_BUTTONS.get(b.get("formType"), "Button"), "form": b.get("formType"), "on": b.get("enabled", True) is not False}
               for b in (cfg.get("ctaButtons") or []) if isinstance(b, dict) and b.get("formType")]
    out = {
        "buttons": [{"label": "My Agent Hub", "form": None, "on": True, "note": "always first"}] + buttons,
        "listings_and_sales_banner": {"on": on_banner, "featured_listing": bool(banner.get("showFeaturedListing", True)),
                                      "recent_sales": bool(banner.get("showRecentSales", True)), "sales_stats": bool(banner.get("showStats", True))},
        "socials_shown": [s for s in SOCIALS if (fields.get(s) or {}).get("include")],
        "details_shown": [label for k, label in SIG_FIELDS.items() if (fields.get(k) or {}).get("include")],
        "look": ({"style": f"Luxe ({cfg.get('luxeMetal') or 'gold'})"} if cfg.get("luxe") else {"accent_color": cfg.get("accentColor"), "font": cfg.get("fontFamily")})
                | {"dark": bool(cfg.get("darkMode")), "follows_brand_kit": cfg.get("lookSource") == "brand", "verified_badge": bool(cfg.get("showVerifiedBadge", True))},
    }
    if html_info:
        out["installed"] = bool(html_info.get("installed"))
        if html_info.get("installed_client"):
            out["installed_in"] = INSTALL_APPS.get(html_info["installed_client"], html_info["installed_client"])
        out["needs_recopy"] = bool(html_info.get("needs_recopy"))
        if html_info.get("needs_recopy"):
            out["why_recopy"] = ("It changed since they copied it - copy it again from the Signature page into their email app."
                                 if html_info.get("recopy_reason") == "edits" else
                                 "Their email app keeps its own copy of the pictures - copy it again to pick up the new photo/listings.")
    else:
        out["installed"] = "unknown"
    out["note"] = "'installed' means they copied it from the Signature page at least once - we can't see inside Gmail/Outlook."
    return out


async def _signature(db: AsyncSession, user: User, args: dict) -> Any:
    ok, sig = await _get(user, "/email-signature/me")
    if not ok:
        return sig
    ok_h, html = await _get(user, "/email-signature/me/html")
    return signature_summary((sig or {}).get("config") or {}, {k: v for k, v in (html or {}).items() if k != "html"} if ok_h and isinstance(html, dict) else None)

LOOKUPS["get_email_signature"] = (
    "What's on the agent's email signature: its buttons (on/off), the listings & sales banner and its parts, which socials and details show, "
    "the look, and whether they've installed it (copied it into Gmail/Outlook) and need to copy it again. Use for 'what's on my signature?' / "
    "'did I install my signature?'.",
    _obj(), SIGNATURE, _signature)


# ================================================================ CALENDAR + CRM (Tools -> Calendar / CRM)

def calendar_summary(data: dict) -> dict:
    out = {}
    for prov, label in (("google", "Google Calendar"), ("microsoft", "Outlook Calendar")):
        st = data.get(f"{prov}_calendar")
        if isinstance(st, dict) and st.get("connected"):
            out[prov] = {"name": label, "connected": True, "account": st.get("account_email"), "last_sync": st.get("last_sync_at"),
                         "needs_reconnect": bool(st.get("needs_reconnect")), **({"last_error": str(st["last_error"])[:200]} if st.get("last_error") else {})}
        else:
            out[prov] = {"name": label, "connected": False, "available": bool(data.get(f"{prov}_calendar_available"))}
    out["connected_any"] = any(v.get("connected") for v in out.values() if isinstance(v, dict))
    out["note"] = ("Connecting (or reconnecting) needs their own Google sign-in on Tools -> Calendar (/studio/calendar). "
                   "resync_calendar pushes everything again. Times are Eastern.")
    return out


def crm_summary(data: dict) -> dict:
    return {
        "follow_up_boss_connected": bool(data.get("crm_fub_connected")),
        **({"follow_up_boss_note": data["crm_fub_note"]} if data.get("crm_fub_note") else {}),
        "webhook_set": bool(data.get("crm_webhook_url")),
        "crm_email_set": bool(data.get("crm_parse_email")),
        "sending_new_leads_on": bool(data.get("crm_push_enabled")),
        "last_lead_sent_at": data.get("crm_last_push_at"),
        "last_send_result": data.get("crm_last_push_status"),
        "portal_forwarding_address": data.get("inbound_address"),
        "note": "Leads OUT = new leads go to their CRM. Leads IN = portal emails forwarded to the forwarding address land in Leads. "
                "send_crm_test_lead sends a test. Keys and a new forwarding address stay on Tools -> CRM (/studio/integrations).",
    }


async def _calendar(db: AsyncSession, user: User, args: dict) -> Any:
    ok, data = await _get(user, "/integrations")
    return calendar_summary(data) if ok and isinstance(data, dict) else data


async def _crm(db: AsyncSession, user: User, args: dict) -> Any:
    ok, data = await _get(user, "/integrations")
    return crm_summary(data) if ok and isinstance(data, dict) else data

LOOKUPS["get_calendar_status"] = (
    "Whether the agent's calendar (Google, Outlook) is connected, which account, when it last synced, and if it needs reconnecting. "
    "Use for 'is my calendar connected?' / 'why isn't it on my calendar?'.", _obj(), None, _calendar)
LOOKUPS["get_crm_status"] = (
    "Whether the agent's CRM is hooked up: Follow Up Boss key, webhook or CRM email set, whether new leads are being sent, the last send "
    "and how it went, and their portal lead forwarding address. Use for 'is my CRM connected?' / 'are my leads going to Follow Up Boss?'.",
    _obj(), None, _crm)


# ================================================================ DISCOVERY FILTER, AI MEDIA (11/05)

async def _discovery_search(db: AsyncSession, user: User, args: dict) -> Any:
    ok, data = await _get(user, "/api/discovery")
    if not ok:
        return data
    p = (data or {}).get("preferences") or {}
    return {k: p.get(k) for k in ("landmark_address", "miles", "min_price", "max_price", "min_bedrooms", "min_bathrooms", "min_square_feet", "brokerages", "cities",
                                  "school_districts", "excluded_agents", "agent_filter_mode")}

LOOKUPS["get_discovery_search"] = (
    "The agent's saved Discovery search as it is right now: the center address, miles, price range, beds and every other filter. The same search the Discovery page shows.",
    _obj(), MLS, _discovery_search)


async def _my_media(db: AsyncSession, user: User, args: dict) -> Any:
    ok, data = await _get(user, "/studio/media-lab/assets", {"limit": 12})
    if not ok:
        return data
    rows = (data or {}).get("assets") or (data or {}).get("items") or []
    return [{k: a.get(k) for k in ("id", "kind", "action", "status", "created_at", "custom_name")} for a in rows]

LOOKUPS["list_my_media"] = ("The agent's newest AI Media projects (photos and videos) and whether each is finished.", _obj(), {"IDX", "PRO_NO_MLS"}, _my_media)
