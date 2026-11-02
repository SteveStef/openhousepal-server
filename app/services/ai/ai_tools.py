"""
The AI Assistant's tool allowlist.

Every executor here is independently owner-scoped - none of them accept an
owner_id/user_id/agent_id from the model. `current_user` always comes from the
authenticated request context passed in by ai_orchestrator_service.py, and every
DB lookup filters by that user's id, the same `Model.owner_id == current_user.id`
pattern used throughout the rest of the app (lead_routes.py, cma_service.py, etc).
A lookup for a record that doesn't belong to the caller returns a generic
"not found" result, never a 403 that would confirm the record exists under
someone else.

Every list-returning tool clamps its `limit` argument to a server-side maximum
(ai_config_service `result_limits`) regardless of what the model requests, and
every tool returns a small, purpose-built dict - never a raw ORM row or an
existing route's full response schema. Property search/detail is the one
exception to owner-scoping: Property rows are shared MLS-sourced listing data
every agent already sees elsewhere in the app, not private per-agent data.

Where an existing route already implements the exact owner-scoping/matching
logic needed (e.g. lead_routes.py's lead<->collection matching), that logic is
imported and reused rather than re-derived - a second, subtly different
implementation of "does this lead belong to this collection" would be a real
place for a tenant-isolation bug to hide.
"""
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import Float, select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import User, Lead, LeadEvent, Collection, Property, OpenHouseEvent, OpenHouseVisitor
from app.api.lead_routes import _fetch_leads, _to_lead_out, _fetch_owner_collections, _match_collections, _load_owned_lead
from app.services.collections_service import CollectionsService
from app.utils.listing_dom import property_days_on_market
from app.services import cma_service, stats_service

logger = get_logger(__name__)


def _esc(s: str) -> str:
    """Literal text inside a LIKE pattern (Postgres escapes with a backslash by default)."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clamp_limit(config: dict, tool_name: str, requested: Optional[int]) -> int:
    maximum = config.get("result_limits", {}).get(tool_name, 10)
    if requested is None:
        return maximum
    try:
        requested = int(requested)
    except (TypeError, ValueError):
        return maximum
    return max(1, min(requested, maximum))


# ---------------------------------------------------------------------------
# Properties (shared MLS-sourced data, not owner-scoped)
# ---------------------------------------------------------------------------

def _property_summary(p: Property) -> dict:
    return {
        "listing_key": p.listing_key,
        "address": p.full_address or p.street_address,
        "city": p.city,
        "state": p.state,
        "price": p.price,
        "bedrooms": p.bedrooms,
        "bathrooms": p.bathrooms,
        "living_area": p.living_area,
        "status": p.home_status,
        "days_on_market": property_days_on_market(p),
    }


_NO_MLS_RESULT = {"error": "not_available", "message": "This agent's plan doesn't include Bright MLS property data, so property search/detail isn't available."}


async def _search_properties(db: AsyncSession, current_user: User, args: dict, config: dict) -> dict:
    # Property rows ARE the Bright MLS mirror - never readable on a No-MLS plan.
    from app.utils.auth import tier_has_mls
    if not tier_has_mls(current_user.plan_tier):
        return dict(_NO_MLS_RESULT)
    query = (args.get("query") or "").strip()
    limit = _clamp_limit(config, "search_properties", args.get("limit"))
    if not query:
        return {"results": [], "count": 0, "note": "No search text provided."}

    # A town name ("Wayne") is that town - not every address containing it (Waynesboro, Wayne Ave)
    # - and never rentals; prices filter here so the answer can't contradict itself (owner's test, 11/01).
    is_town = not any(ch.isdigit() for ch in query)
    # An address however it's said ("221 Conestoga Road", "221 Conestoga Rd in Wayne"): the MLS keeps
    # "221 CONESTOGA RD", so the house number + street name also match on their own (11/01).
    import re as _re
    num_street = _re.match(r"\s*(\d+[A-Za-z]?)\s+([A-Za-z0-9']{2,})", query)
    # Every match here must use an index (trigram on full_address / city, listing_key): one
    # unindexed street_address match made Postgres read all ~1.1M live rows per search, so an
    # address question ran past the chat's 60 s limit (owner, 11/01 - 735 Righters Mill Rd).
    # full_address starts with the street address, so it covers both.
    by_street = [Property.full_address.ilike(f"{_esc(num_street.group(1))} {_esc(num_street.group(2))}%")] if num_street else []
    where = [or_(Property.city.ilike(_esc(query)), Property.listing_key == query) if is_town
             else or_(Property.full_address.ilike(f"%{_esc(query)}%"), Property.listing_key == query, *by_street)]
    lease = Property.raw_mls_data["PropertyType"].astext
    where.append(or_(lease.is_(None), ~lease.ilike("%lease%")))
    if args.get("for_sale_only", True):
        where.append(or_(Property.home_status.ilike("%active%"), Property.home_status.ilike("%coming%")))
    for key, op in (("min_price", "__ge__"), ("max_price", "__le__")):
        try:
            if args.get(key) not in (None, ""):
                where.append(getattr(Property.price, op)(float(args[key])))
        except (TypeError, ValueError):
            pass
    stmt = (
        select(Property)
        .where(*where)
        .order_by(Property.mls_list_date.desc().nullslast())
        .limit(limit)
    )
    result = await db.execute(stmt)
    properties = result.scalars().all()
    return {"results": [_property_summary(p) for p in properties], "count": len(properties)}


async def _get_property_detail(db: AsyncSession, current_user: User, args: dict, config: dict) -> dict:
    from app.utils.auth import tier_has_mls
    if not tier_has_mls(current_user.plan_tier):
        return dict(_NO_MLS_RESULT)
    listing_key = (args.get("listing_key") or "").strip()
    if not listing_key:
        return {"error": "not_found", "message": "No listing_key provided."}

    stmt = select(Property).where(Property.listing_key == listing_key)
    result = await db.execute(stmt)
    p = result.scalar_one_or_none()
    if not p:
        return {"error": "not_found", "message": "No property found with that listing key."}

    return {
        **_property_summary(p),
        "listing_id": p.listing_id,
        "description": p.description,
        "year_built": p.year_built,
        "lot_size": p.lot_size,
        "home_type": p.home_type,
        "list_agent_full_name": p.list_agent_full_name,
        "list_office_name": p.list_office_name,
        "tax_annual_amount": p.tax_annual_amount,
        "original_list_price": p.original_list_price,
        "cumulative_days_on_market": p.cumulative_days_on_market,
        "school_district_name": p.school_district_name,
        "county": p.county,
        "township": p.township,
        "subdivision_name": p.subdivision_name,
        "elementary_school": p.elementary_school,
        "middle_or_junior_school": p.middle_or_junior_school,
        "high_school": p.high_school,
    }


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------

def _lead_summary(lo) -> dict:
    return {
        "id": lo.id,
        "name": lo.name,
        "phone": lo.phone,  # included here (not just get_lead_detail) so "who should I text"
                             # questions resolve in a single search_leads call, not two round-trips
        "email": lo.email,
        "status": lo.status,
        "represented": lo.represented,
        "tags": lo.tags,
        "primary_source": lo.primary_source,
        "last_event_summary": lo.last_event_summary,
        "last_event_at": lo.last_event_at.isoformat() if lo.last_event_at else None,
        "last_contacted_at": lo.last_contacted_at.isoformat() if lo.last_contacted_at else None,
        "needs_attention": lo.needs_attention,
        "showcase_new_matches": lo.showcase_new_matches,
        "has_liked_property": lo.has_liked_property,
        "has_tour_request": lo.has_tour_request,
        "has_commented": lo.has_commented,
    }


async def _search_leads(db: AsyncSession, current_user: User, args: dict, config: dict) -> dict:
    limit = _clamp_limit(config, "search_leads", args.get("limit"))
    status = args.get("status")
    represented = args.get("represented")
    search = args.get("search")

    leads = await _fetch_leads(db, current_user, status, represented, search, hidden=False)
    collections = await _fetch_owner_collections(db, current_user.id)
    leads_out = [_to_lead_out(l, collections) for l in leads]

    # Unrepresented leads first (an already-represented lead isn't this agent's to pursue),
    # then needs-attention, then most recently active - the single most useful default order
    # for "who should I follow up with" without needing a dedicated sort tool parameter.
    def sort_key(lo):
        last = lo.last_event_at or lo.created_at
        return (lo.represented == 'yes', not lo.needs_attention, -(last.timestamp() if last else 0))

    leads_out.sort(key=sort_key)
    shown = leads_out[:limit]
    return {"results": [_lead_summary(lo) for lo in shown], "count": len(leads_out), "shown": len(shown)}


async def _get_lead_detail(db: AsyncSession, current_user: User, args: dict, config: dict) -> dict:
    lead_id = (args.get("lead_id") or "").strip()
    if not lead_id:
        return {"error": "not_found", "message": "No lead_id provided."}

    try:
        lead = await _load_owned_lead(db, lead_id, current_user.id)
    except HTTPException:
        return {"error": "not_found", "message": "No lead found with that id."}

    collections = await _fetch_owner_collections(db, current_user.id)
    lo = _to_lead_out(lead, collections)

    summary = _lead_summary(lo)
    summary.update({
        "email": lo.email,
        "phone": lo.phone,
        "agent_notes_legacy": lo.notes,
        "created_at": lo.created_at.isoformat() if lo.created_at else None,
        "property_address": lo.property_address,
        "source_count": lo.source_count,
    })

    events = sorted(lead.events or [], key=lambda e: e.occurred_at, reverse=True)[:15]
    summary["recent_events"] = [
        {
            "source_type": e.source_type,
            "summary": e.summary,
            "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
            "property_address": e.property_address,
        }
        for e in events
    ]

    notes = sorted(lead.lead_notes or [], key=lambda n: n.created_at, reverse=True)[:10]
    summary["agent_notes"] = [
        {"text": n.text, "created_at": n.created_at.isoformat() if n.created_at else None} for n in notes
    ]
    return summary


# ---------------------------------------------------------------------------
# Open houses
# ---------------------------------------------------------------------------

async def _search_open_houses(db: AsyncSession, current_user: User, args: dict, config: dict) -> dict:
    limit = _clamp_limit(config, "search_open_houses", args.get("limit"))
    search = (args.get("search") or "").strip()

    stmt = select(OpenHouseEvent).where(OpenHouseEvent.agent_id == current_user.id, OpenHouseEvent.is_deleted.is_(False))
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(OpenHouseEvent.address.ilike(like), OpenHouseEvent.city.ilike(like)))
    stmt = stmt.order_by(OpenHouseEvent.scheduled_date.desc().nullslast(), OpenHouseEvent.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    events = result.scalars().all()

    if not events:
        return {"results": [], "count": 0}

    ids = [e.id for e in events]
    count_stmt = (
        select(OpenHouseVisitor.open_house_event_id, func.count(OpenHouseVisitor.id))
        .where(OpenHouseVisitor.open_house_event_id.in_(ids))
        .group_by(OpenHouseVisitor.open_house_event_id)
    )
    count_result = await db.execute(count_stmt)
    visitor_counts = dict(count_result.all())
    # Kits are undated (evergreen) - the days guests actually signed in are the open house dates.
    days_rows = (await db.execute(
        select(OpenHouseVisitor.open_house_event_id, func.min(OpenHouseVisitor.created_at), func.max(OpenHouseVisitor.created_at))
        .where(OpenHouseVisitor.open_house_event_id.in_(ids)).group_by(OpenHouseVisitor.open_house_event_id))).all()
    signin_days = {r[0]: (r[1], r[2]) for r in days_rows}

    return {
        "results": [
            {
                "id": e.id,
                "address": e.address,
                "city": e.city,
                "scheduled_date": e.scheduled_date.isoformat() if e.scheduled_date else None,
                "is_active": e.is_active,
                "visitor_count": visitor_counts.get(e.id, 0),
                "first_sign_in": signin_days[e.id][0].isoformat() if e.id in signin_days and signin_days[e.id][0] else None,
                "last_sign_in": signin_days[e.id][1].isoformat() if e.id in signin_days and signin_days[e.id][1] else None,
                "price": e.price,
            }
            for e in events
        ],
        "count": len(events),
    }


async def _get_open_house_guests(db: AsyncSession, current_user: User, args: dict, config: dict) -> dict:
    open_house_id = (args.get("open_house_id") or "").strip()
    limit = _clamp_limit(config, "get_open_house_guests", args.get("limit"))
    if not open_house_id:
        return {"error": "not_found", "message": "No open_house_id provided."}

    event_stmt = select(OpenHouseEvent).where(OpenHouseEvent.id == open_house_id, OpenHouseEvent.agent_id == current_user.id)
    event_result = await db.execute(event_stmt)
    event = event_result.scalar_one_or_none()
    if not event:
        return {"error": "not_found", "message": "No open house found with that id belonging to this agent."}

    stmt = (
        select(OpenHouseVisitor)
        .where(OpenHouseVisitor.open_house_event_id == open_house_id)
        .order_by(OpenHouseVisitor.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    visitors = result.scalars().all()

    return {
        "open_house_address": event.address,
        "results": [
            {
                "visitor_id": v.id,   # for the helper's per-guest actions (10/30)
                "lead_id": v.lead_id,
                "name": v.full_name,
                "email": v.email,
                "phone": v.phone,
                "has_agent": v.has_agent,
                "interested_in_similar": v.interested_in_similar,
                "signed_in_at": v.created_at.isoformat() if v.created_at else None,
            }
            for v in visitors
        ],
        "count": len(visitors),
    }


async def _find_open_house_guests(db: AsyncSession, current_user: User, args: dict, config: dict) -> dict:
    """Guests across ALL of the agent's open houses at once - by name, email or phone, and/or by when
    they signed in (owner, 11/01: "a guest named Matt at one of my open houses last week" made the
    helper open each sign-in list one by one, run out of steps and stop at "I'll check...")."""
    from datetime import datetime, timedelta, timezone
    name = (args.get("name") or "").strip()
    days = args.get("days_back")
    try:
        days = max(1, min(400, int(days))) if days is not None else None
    except (TypeError, ValueError):
        days = None
    stmt = (select(OpenHouseVisitor, OpenHouseEvent.address, OpenHouseEvent.city, OpenHouseEvent.id)
            .join(OpenHouseEvent, OpenHouseEvent.id == OpenHouseVisitor.open_house_event_id)
            .where(OpenHouseEvent.agent_id == current_user.id, OpenHouseEvent.is_deleted.is_(False)))
    if name:
        words = [w for w in name.split() if w][:3]
        for w in words:
            like = f"%{w}%"
            stmt = stmt.where(or_(OpenHouseVisitor.full_name.ilike(like), OpenHouseVisitor.email.ilike(like), OpenHouseVisitor.phone.ilike(like)))
    if days:
        stmt = stmt.where(OpenHouseVisitor.created_at >= datetime.now(timezone.utc) - timedelta(days=days))
    rows = (await db.execute(stmt.order_by(OpenHouseVisitor.created_at.desc()).limit(40))).all()
    return {
        "results": [{
            "name": v.full_name, "email": v.email, "phone": v.phone, "has_agent": v.has_agent,
            "signed_in_at": v.created_at.isoformat() if v.created_at else None,
            "open_house": ", ".join(x for x in (addr, city) if x), "open_house_id": oh_id,
            "visitor_id": v.id, "lead_id": v.lead_id,
            "lead_page": f"/leads/{v.lead_id}" if v.lead_id else None,
        } for v, addr, city, oh_id in rows],
        "count": len(rows),
    }


async def _get_similar_properties(db: AsyncSession, current_user: User, args: dict, config: dict) -> dict:
    open_house_id = (args.get("open_house_id") or "").strip()
    limit = _clamp_limit(config, "get_similar_properties", args.get("limit"))
    if not open_house_id:
        return {"error": "not_found", "message": "No open_house_id provided."}

    stmt = select(OpenHouseEvent).where(OpenHouseEvent.id == open_house_id, OpenHouseEvent.agent_id == current_user.id)
    result = await db.execute(stmt)
    event = result.scalar_one_or_none()
    if not event:
        return {"error": "not_found", "message": "No open house found with that id belonging to this agent."}

    snapshot = event.similar_properties_snapshot
    if not isinstance(snapshot, list):
        return {"results": [], "count": 0, "note": "No Similar Properties have been saved for this open house."}

    items = []
    for entry in snapshot[:limit]:
        if not isinstance(entry, dict):
            continue
        items.append({
            "address": entry.get("street_address") or entry.get("address") or entry.get("full_address"),
            "city": entry.get("city"),
            "price": entry.get("price"),
            "bedrooms": entry.get("bedrooms"),
            "bathrooms": entry.get("bathrooms"),
            "living_area": entry.get("living_area"),
            "listing_key": entry.get("listing_key"),
        })
    return {"results": items, "count": len(items)}


# ---------------------------------------------------------------------------
# Showcases / matches (per-lead Collection)
# ---------------------------------------------------------------------------

async def _find_lead_collection(db: AsyncSession, current_user: User, lead_id: str) -> Tuple[Optional[Lead], Optional[Collection]]:
    try:
        lead = await _load_owned_lead(db, lead_id, current_user.id)
    except HTTPException:
        return None, None
    collections = await _fetch_owner_collections(db, current_user.id)
    matched = _match_collections(lead, collections)
    return lead, (matched[0] if matched else None)


async def _get_property_matches(db: AsyncSession, current_user: User, args: dict, config: dict) -> dict:
    lead_id = (args.get("lead_id") or "").strip()
    limit = _clamp_limit(config, "get_property_matches", args.get("limit"))
    if not lead_id:
        return {"error": "not_found", "message": "No lead_id provided."}

    lead, collection = await _find_lead_collection(db, current_user, lead_id)
    if not lead:
        return {"error": "not_found", "message": "No lead found with that id."}
    if not collection:
        return {"has_showcase": False, "results": [], "count": 0}

    properties = list(collection.properties or [])
    return {
        "has_showcase": True,
        "collection_id": collection.id,
        "new_match_count": CollectionsService.count_new_properties(collection),
        "results": [_property_summary(p) for p in properties[:limit]],
        "count": len(properties),
    }


async def _get_showcase_info(db: AsyncSession, current_user: User, args: dict, config: dict) -> dict:
    lead_id = (args.get("lead_id") or "").strip()
    if not lead_id:
        return {"error": "not_found", "message": "No lead_id provided."}

    lead, collection = await _find_lead_collection(db, current_user, lead_id)
    if not lead:
        return {"error": "not_found", "message": "No lead found with that id."}
    if not collection:
        return {"has_showcase": False}

    return {
        "has_showcase": True,
        "collection_id": collection.id,
        "status": collection.status,
        "is_shared": bool(collection.is_public and collection.share_token),
        "property_count": len(collection.properties or []),
        "new_match_count": CollectionsService.count_new_properties(collection),
        "last_visitor_activity_at": collection.last_visitor_activity_at.isoformat() if collection.last_visitor_activity_at else None,
        "created_at": collection.created_at.isoformat() if collection.created_at else None,
    }


# ---------------------------------------------------------------------------
# CMAs
# ---------------------------------------------------------------------------

async def _get_cma_summary(db: AsyncSession, current_user: User, args: dict, config: dict) -> dict:
    report_id = (args.get("report_id") or "").strip()

    if report_id:
        try:
            report = await cma_service.get_report(db, current_user.id, report_id)
        except HTTPException:
            return {"error": "not_found", "message": "No CMA report found with that id."}

        subject = report.subject or {}
        pricing = report.pricing or {}
        comparables = [c for c in (report.comparables or []) if c.get("is_included", True)]
        address = ", ".join(p for p in [subject.get("street_address"), subject.get("city"), subject.get("state")] if p) or None
        return {
            "id": report.id,
            "client_name": report.client_name,
            "mode": report.mode,
            "status": report.status,
            "subject_address": address,
            "subject_price": subject.get("price"),
            "comparable_count": len(comparables),
            "value_range_min": pricing.get("recommended_min") or pricing.get("comparable_suggested_min"),
            "value_range_max": pricing.get("recommended_max") or pricing.get("comparable_suggested_max"),
            "pricing_commentary": pricing.get("commentary"),
            "updated_at": report.updated_at.isoformat() if report.updated_at else None,
        }

    limit = _clamp_limit(config, "get_cma_summary", args.get("limit"))
    reports = await cma_service.list_reports(db, current_user.id)
    shown = reports[:limit]
    return {
        "results": [
            {
                "id": r.id,
                "client_name": r.client_name,
                "status": r.status,
                "subject_address": r.subject_address,
                "value_range_min": r.value_range_min,
                "value_range_max": r.value_range_max,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in shown
        ],
        "count": len(reports),
        "shown": len(shown),
    }


# ---------------------------------------------------------------------------
# Activity / stats
# ---------------------------------------------------------------------------

async def _get_agent_activity_summary(db: AsyncSession, current_user: User, args: dict, config: dict) -> dict:
    limit = _clamp_limit(config, "list_recent_activity", args.get("limit"))

    stats = await stats_service.get_summary(db, current_user.id)

    stmt = (
        select(LeadEvent, Lead.name)
        .join(Lead, Lead.id == LeadEvent.lead_id)
        .where(Lead.owner_id == current_user.id)
        .order_by(LeadEvent.occurred_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = result.all()

    return {
        "stats": stats,
        "recent_activity": [
            {
                "lead_id": event.lead_id,
                "lead_name": lead_name,
                "source_type": event.source_type,
                "summary": event.summary,
                "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
            }
            for event, lead_name in rows
        ],
    }


async def _get_priority_summary(db: AsyncSession, current_user: User, args: dict, config: dict) -> dict:
    """Zero-LLM-cost, pre-computed priority list - the exact same logic behind the
    Dashboard's Daily Digest (ai_insights_service.get_insights). Reusing it here means
    "who should I follow up with" gets an already-correct, already-phrased answer instead
    of the model re-deriving recency/priority from a raw lead+timestamp dump itself."""
    from app.services.ai import ai_insights_service
    priorities = await ai_insights_service.get_insights(db, current_user)
    return {"priorities": priorities}


async def _look_up_agent(db: AsyncSession, current_user: User, args: dict, config: dict) -> dict:
    """Another agent by name, from the MLS records (owner, 11/01: "how many homes has Lavinia
    Smerconish sold and what are her listings - I might ask to host an open house around $2M"):
    each matching agent's office and contact, sales in the last 12 months (listing side and buyer
    side, with volume) and their listings for sale now - priced in range when one is given."""
    from datetime import date, timedelta
    from app.utils.auth import tier_has_mls
    if not tier_has_mls(current_user.plan_tier):
        return dict(_NO_MLS_RESULT)
    name = " ".join(str(args.get("name") or "").split())
    if len(name) < 3:
        return {"error": "need_name", "message": "Give the agent's name."}
    like = f"%{name}%"
    since = (date.today() - timedelta(days=365)).isoformat()
    raw = Property.raw_mls_data
    close_date, close_price = raw["CloseDate"].astext, raw["ClosePrice"].astext
    closed = Property.home_status == "CLOSED-BRIGHT"

    # who: agents whose name matches (the listing side's name is a column), then everything by their
    # MLS id - indexed, so it stays quick on the full MLS table (a jsonb name scan took ~4s locally).
    from sqlalchemy import literal_column
    names = (await db.execute(
        select(Property.list_agent_full_name, func.max(Property.list_agent_mls_id), func.max(Property.list_office_name),
               func.max(Property.list_agent_email), func.max(raw["ListAgentPreferredPhone"].astext), func.count())
        .where(Property.list_agent_full_name.ilike(like)).group_by(Property.list_agent_full_name)
        .order_by(func.count().desc()).limit(3))).all()
    agents = [{"name": full, "mls_id": mid, "office": office, "email": email, "phone": phone} for full, mid, office, email, phone, _n in names if full]
    if not agents:
        # only ever a buyer's agent here: the slower name scan, on sold homes only
        buyer_col = literal_column("properties.raw_mls_data->>'BuyerAgentFullName'")
        rows = (await db.execute(
            select(buyer_col, func.max(raw["BuyerAgentMlsId"].astext), func.max(raw["BuyerOfficeName"].astext))
            .where(closed, buyer_col.ilike(like)).group_by(buyer_col).limit(3))).all()
        agents = [{"name": full, "mls_id": mid, "office": office} for full, mid, office in rows if full]
    if not agents:
        return {"agents": [], "note": f"No agent named like '{name}' in the MLS records here. Check the spelling, or ask for their town or office."}

    lo, hi = args.get("min_price"), args.get("max_price")
    out = []
    for a in agents:
        mid, full = a.pop("mls_id", None), a["name"]
        mine = Property.list_agent_mls_id == mid if mid else Property.list_agent_full_name == full
        listing_sold = (await db.execute(
            select(func.count(), func.coalesce(func.sum(func.cast(close_price, Float)), 0))
            .where(closed, mine, close_date >= since))).one()
        buyer_sold = (await db.execute(
            select(func.count(), func.coalesce(func.sum(func.cast(close_price, Float)), 0))
            .where(closed, raw["BuyerAgentMlsId"].astext == mid, close_date >= since))).one() if mid else (0, 0)
        where = [mine, or_(Property.home_status.ilike("%active%"), Property.home_status.ilike("%coming%")),
                 ~Property.home_status.ilike("%under contract%")]
        try:
            if lo not in (None, ""):
                where.append(Property.price >= float(lo))
            if hi not in (None, ""):
                where.append(Property.price <= float(hi))
        except (TypeError, ValueError):
            pass
        rows = (await db.execute(select(Property).where(*where).order_by(Property.price.desc().nullslast()).limit(12))).scalars().all()
        recent_sales = (await db.execute(
            select(Property.street_address, Property.city, close_price, close_date)
            .where(closed, mine, close_date >= since).order_by(close_date.desc()).limit(5))).all()
        out.append({
            **a,
            "sold_last_12_months_as_listing_agent": int(listing_sold[0] or 0), "listing_side_volume": round(float(listing_sold[1] or 0)),
            "sold_last_12_months_as_buyer_agent": int(buyer_sold[0] or 0), "buyer_side_volume": round(float(buyer_sold[1] or 0)),
            "listings_for_sale_now": [_property_summary(p) for p in rows],
            "recent_sales": [{"address": f"{st}, {c}" if c else st, "sold_price": float(pr) if pr else None, "closed": cd} for st, c, pr, cd in recent_sales],
        })
    return {"agents": out, "price_range": {"min": lo, "max": hi},
            "note": "From the MLS records OpenHousePal keeps (about the last 12 months of sales). To host at one of their listings, use request_to_host with its listing_key."}


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "name": "look_up_agent",
        "description": "Look up ANOTHER agent by name in the MLS records: their office, email/phone, how many homes they sold in the last 12 months (as listing agent and as buyer's agent, with volume), recent sales, and their listings for sale now (optionally in a price range). Use it for 'how many homes has X sold', 'what is X listing', 'whose listing is this', finding a listing to ask to host an open house at.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The agent's name as said ('Lavinia Smerconish')."},
                "min_price": {"type": "number"}, "max_price": {"type": "number"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "search_properties",
        "description": "Search OpenHousePal's property database by address or city text. Returns compact summaries for multiple properties - use get_property_detail for one specific property's full facts. Property data is shared MLS-sourced listing data, not private to any one agent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "An address ('26 Old Gulph Road') or ONE town name exactly ('Villanova')."},
                "limit": {"type": "integer", "description": "Max results to return."},
                "min_price": {"type": "number"}, "max_price": {"type": "number"},
                "for_sale_only": {"type": "boolean", "description": "Default true: only homes for sale now (active / coming soon). false to include sold and off-market."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_property_detail",
        "description": "Get full factual details for one specific property by its listing_key (obtained from search_properties or another tool result).",
        "input_schema": {
            "type": "object",
            "properties": {"listing_key": {"type": "string"}},
            "required": ["listing_key"],
        },
    },
    {
        "name": "search_leads",
        "description": "Search the logged-in agent's own leads. Only ever returns leads belonging to this agent. Results are sorted with unrepresented leads (represented 'no' or 'unknown') first - a lead who already has an agent isn't this agent's to pursue - then leads needing attention, then most recently active. Pass represented='yes' explicitly if the agent specifically asks about already-represented leads.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["new", "contacted", "in_talks", "signed", "all"]},
                "represented": {"type": "string", "enum": ["yes", "no", "unknown", "all"]},
                "search": {"type": "string", "description": "Free-text search over name/email/phone."},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "get_lead_detail",
        "description": "Get full details for one specific lead the agent owns, including recent activity events and the agent's own notes on that lead. Only returns data if the lead belongs to the logged-in agent.",
        "input_schema": {
            "type": "object",
            "properties": {"lead_id": {"type": "string"}},
            "required": ["lead_id"],
        },
    },
    {
        "name": "search_open_houses",
        "description": "Search the logged-in agent's own open houses, past and upcoming. Kits aren't dated - first_sign_in / last_sign_in say when guests actually came.",
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Address or city text."},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "find_open_house_guests",
        "description": "Find guests across ALL of the agent's open houses in one call - by name (first, last or part of it), email or phone, "
                       "and/or by how many days back they signed in (e.g. 'Matt last week' = name 'Matt', days_back 10). Returns each match with "
                       "the open house, the sign-in date and their lead page. Use this instead of opening sign-in lists one by one.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "days_back": {"type": "integer"}},
        },
    },
    {
        "name": "get_open_house_guests",
        "description": "Get the visitor sign-in list for one of the agent's own open houses.",
        "input_schema": {
            "type": "object",
            "properties": {"open_house_id": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["open_house_id"],
        },
    },
    {
        "name": "get_property_matches",
        "description": "Get the properties matched to one of the agent's own leads via that lead's saved Showcase (property collection), including how many are new since the agent last checked.",
        "input_schema": {
            "type": "object",
            "properties": {"lead_id": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["lead_id"],
        },
    },
    {
        "name": "get_showcase_info",
        "description": "Check whether one of the agent's own leads has an OpenHousePal Showcase (property collection) and its engagement stats - shared status, property count, last visitor activity.",
        "input_schema": {
            "type": "object",
            "properties": {"lead_id": {"type": "string"}},
            "required": ["lead_id"],
        },
    },
    {
        "name": "get_similar_properties",
        "description": "Get the Similar Properties saved for one of the agent's own open houses.",
        "input_schema": {
            "type": "object",
            "properties": {"open_house_id": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["open_house_id"],
        },
    },
    {
        "name": "get_cma_summary",
        "description": "Get a summary of one of the agent's own CMA (Comparative Market Analysis) reports by report_id, or a list of the agent's recent CMA reports if report_id is omitted.",
        "input_schema": {
            "type": "object",
            "properties": {"report_id": {"type": "string"}, "limit": {"type": "integer"}},
        },
    },
    {
        "name": "get_agent_activity_summary",
        "description": "Get the logged-in agent's headline stats (leads, opens, visitors) plus their most recent activity feed. Good starting point for broad questions like 'what's happened since yesterday' or 'prepare me for today'.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        },
    },
    {
        "name": "get_priority_summary",
        "description": "Get the same prioritized 'what needs attention today' list shown on the agent's Dashboard - leads needing a look, leads with unseen Showcase matches (with phone number and suggested contact), and open houses coming up in the next few days. Pre-computed and always accurate, zero cost. Call this FIRST for broad questions like 'who should I follow up with', 'what's important today', or 'what should I focus on' - only fall back to search_leads or get_agent_activity_summary if the agent wants more than this returns.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]

_DISPATCH: Dict[str, Callable] = {
    "search_properties": _search_properties,
    "look_up_agent": _look_up_agent,
    "get_property_detail": _get_property_detail,
    "search_leads": _search_leads,
    "get_lead_detail": _get_lead_detail,
    "search_open_houses": _search_open_houses,
    "get_open_house_guests": _get_open_house_guests,
    "find_open_house_guests": _find_open_house_guests,
    "get_property_matches": _get_property_matches,
    "get_showcase_info": _get_showcase_info,
    "get_similar_properties": _get_similar_properties,
    "get_cma_summary": _get_cma_summary,
    "get_agent_activity_summary": _get_agent_activity_summary,
    "get_priority_summary": _get_priority_summary,
}

# Keys the model must never be able to influence, even if it hallucinates them into a tool
# call. Every executor above already ignores these by construction - none of them read from
# `args` to determine whose data to query, only `current_user` from server context. This is a
# second, defense-in-depth strip so a stray key can't do anything even if a future executor
# were written carelessly.
_FORBIDDEN_ARG_KEYS = {"owner_id", "user_id", "agent_id", "current_user_id"}


# MLS listing lookups follow the app's own require_mls_access: a No-MLS plan never gets Bright
# data through the helper either (owner, 10/30 - the helper opened to every plan).
_MLS_TOOLS = {"search_properties", "get_property_detail", "get_similar_properties"}


def _allowed(current_user: User, tool_name: str) -> bool:
    from app.utils.auth import MLS_ACCESS_TIERS
    # ...and like the app's MLS pages (require_broker_authorization), only once their broker approved them.
    return tool_name not in _MLS_TOOLS or current_user.is_admin or (current_user.plan_tier in MLS_ACCESS_TIERS and bool(current_user.broker_authorized))


def tools_for(current_user: User) -> list:
    """The tool list this agent's plan gets - what the model is even told exists. The typed
    helper also gets ai_lookups (the app's own pages' GETs, 10/30); voice keeps the core set."""
    from app.services.ai import ai_lookups
    return [t for t in TOOL_DEFINITIONS if _allowed(current_user, t["name"])] + ai_lookups.definitions_for(current_user)


async def execute_tool(db: AsyncSession, current_user: User, tool_name: str, raw_args: dict, config: dict) -> Dict[str, Any]:
    executor = _DISPATCH.get(tool_name)
    if executor and not _allowed(current_user, tool_name):
        return {"error": "not_on_plan", "message": "MLS listing data isn't part of this agent's plan."}
    if not executor:
        from app.services.ai import ai_lookups
        if tool_name in ai_lookups.LOOKUPS:
            args = {k: v for k, v in (raw_args or {}).items() if k not in _FORBIDDEN_ARG_KEYS}
            try:
                return await ai_lookups.run_lookup(db, current_user, tool_name, args)
            except Exception:
                logger.exception("AI lookup '%s' failed for user %s", tool_name, current_user.id)
                return {"error": "tool_failed", "message": "Something went wrong retrieving that information."}
        return {"error": "unknown_tool", "message": f"Tool '{tool_name}' does not exist."}

    args = {k: v for k, v in (raw_args or {}).items() if k not in _FORBIDDEN_ARG_KEYS}

    try:
        return await executor(db, current_user, args, config)
    except Exception:
        logger.exception("AI tool '%s' failed for user %s", tool_name, current_user.id)
        return {"error": "tool_failed", "message": "Something went wrong retrieving that information."}
