"""Admin "Neighborhoods" tab (owner, 10/26): every shared town record (MarketArea) - search them,
see exactly what its IDX town page shows, and fix or add what it says.

A town is shared by every agent who serves it, so an edit here changes the page on every one of
their sites. What the admin can change:
- the town write-up (narrative.summary);
- each place list (downtown, restaurants, shopping, grocery, parks, transportation, medical,
  private schools): edit, take off, reorder, add, or put back a place the checks took off;
- the public schools by level.
Every change is marked (`admin_edited` on the place, `_admin_edited_at` on the town) so the Google
place check (town_places_check) leaves it alone and town seeding treats the town as finished. Edited
text goes through the Fair Housing wording rules (fair_housing_service.fix_area_text, fixed rules
only). After a save the town's cached copies are dropped so the page shows the change at once
(the frontend also expires its own 30-second copy - app/api/website-areas/revalidate)."""
from __future__ import annotations

import asyncio
import copy
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.database import get_db
from app.models.database import AgentMarketArea, MarketArea, Site, User
from app.services.areas.fair_housing_service import clean_area_payload, clean_text, fix_area_text
from app.services.areas.town_places_check import CHECK_CATEGORIES, medical_label
from app.utils.auth import require_admin_user

router = APIRouter(prefix="/admin/areas", tags=["admin-areas"])

# What one "Research again" costs in AI (owner's measured average for a full town research).
RESEARCH_COST_USD = 0.66

# The place lists in the order the town page shows them, with the page's own headings.
PLACE_LISTS: list[tuple[str, str, str]] = [
    ("private_schools", "Private schools", "Schools"),
    ("downtown_centers", "Downtown & town centers", "Living here"),
    ("restaurants", "Restaurants", "Living here"),
    ("shopping", "Shopping", "Living here"),
    ("grocery", "Grocery", "Living here"),
    ("parks", "Parks & recreation", "Living here"),
    ("transportation", "Transportation", "Getting around"),
    ("medical", "Medical & hospitals", "Getting around"),
]
PLACE_KEYS = {k for k, _, _ in PLACE_LISTS}
# The "Living here" tabs show the first 9 of a list, the first one big as a "Local favorite" (TownLiving.tsx).
LIVING_PAGE_LIMIT = 9
SCHOOL_LEVELS = (("elementary_schools", "Elementary"), ("middle_schools", "Middle"), ("high_schools", "High school"))
EDIT_FIELDS = {"name": 200, "category": 120, "city": 100, "address": 300, "description": 1200, "source_url": 600}
STATUSES = ("ENRICHED", "PARTIAL", "PENDING", "RESEARCHING", "FAILED")

_busy: set[str] = set()  # towns being checked on Google right now


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(d) -> Optional[str]:
    return d.isoformat() if d else None


def _clean_field(field: str, value) -> Optional[str]:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text:
        return None
    if len(text) > EDIT_FIELDS[field]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"That {field.replace('_', ' ')} is too long - keep it under {EDIT_FIELDS[field]} characters.")
    if field == "source_url" and not re.match(r"^https?://", text, re.IGNORECASE):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A web link has to start with http:// or https://")
    return text


def _loose(value) -> Optional[str]:
    """A stored value the way the editor sends it back (spaces tidied, empty = None)."""
    text = re.sub(r"\s+", " ", str(value)).strip() if value is not None else ""
    return text or None


def _place_count(facts: dict) -> int:
    return sum(len((facts.get(k) or {}).get("entities") or []) for k in PLACE_KEYS if isinstance(facts.get(k), dict))


def _removed_count(facts: dict) -> int:
    return sum(len((facts.get(k) or {}).get("_removed") or []) for k in PLACE_KEYS if isinstance(facts.get(k), dict))


# ---------------------------------------------------------------- search

async def search_areas(db: AsyncSession, q: str = "", state: str = "", area_type: str = "", research: str = "", used: str = "",
                       sort: str = "name", page: int = 1, page_size: int = 25) -> dict:
    agents = (select(AgentMarketArea.area_id, func.count(AgentMarketArea.id).label("n"))
              .group_by(AgentMarketArea.area_id).subquery())
    n_agents = func.coalesce(agents.c.n, 0)
    stmt = select(MarketArea, n_agents.label("agents")).outerjoin(agents, agents.c.area_id == MarketArea.id)
    words = [w for w in re.split(r"[\s,]+", (q or "").strip()) if w]
    for w in words:
        like = f"%{w}%"
        stmt = stmt.where(or_(MarketArea.name.ilike(like), MarketArea.slug.ilike(like), MarketArea.parent_area_name.ilike(like),
                              MarketArea.county.ilike(like), MarketArea.state.ilike(w)))
    if state:
        stmt = stmt.where(MarketArea.state == state.upper()[:2])
    if area_type in ("city", "neighborhood"):
        stmt = stmt.where(MarketArea.area_type == area_type)
    if research in STATUSES:
        stmt = stmt.where(MarketArea.enrichment_status == research)
    if used == "yes":
        stmt = stmt.where(n_agents > 0)
    elif used == "no":
        stmt = stmt.where(n_agents == 0)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    order = {
        "agents": (n_agents.desc(), MarketArea.name),
        "researched": (MarketArea.last_enriched_at.desc().nullslast(), MarketArea.name),
    }.get(sort, (MarketArea.name, MarketArea.state))
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    rows = (await db.execute(stmt.order_by(*order).offset((page - 1) * page_size).limit(page_size))).all()
    states = (await db.execute(select(MarketArea.state).distinct().order_by(MarketArea.state))).scalars().all()
    items = []
    for area, n in rows:
        facts = area.facts or {}
        items.append({
            "id": area.id, "name": area.name, "state": area.state, "slug": area.slug, "county": area.county,
            "area_type": area.area_type, "parent_area_name": area.parent_area_name,
            "enrichment_status": area.enrichment_status, "last_enriched_at": _iso(area.last_enriched_at),
            "agent_count": int(n or 0),
            "places_checked": bool(facts.get("_places_check_version")),
            "places_checked_at": facts.get("_places_verified_at"),
            "places_waiting": bool(facts.get("_places_check_waiting")),
            "place_count": _place_count(facts), "removed_count": _removed_count(facts),
            "has_summary": bool(((area.narrative or {}).get("summary") or "").strip()),
            "admin_edited_at": facts.get("_admin_edited_at"),
        })
    return {"items": items, "total": total, "page": page, "page_size": page_size, "states": [s for s in states if s]}


@router.get("")
async def list_areas(
    q: str = Query("", max_length=120), state: str = Query("", max_length=2), area_type: str = Query("", max_length=20),
    research: str = Query("", max_length=20), used: str = Query("", max_length=3), sort: str = Query("name", max_length=20),
    page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin_user),
):
    return await search_areas(db, q, state, area_type, research, used, sort, page, page_size)


# ---------------------------------------------------------------- detail

async def _agents_for(db: AsyncSession, area: MarketArea) -> list[dict]:
    from app.api.public_site_routes import _idx_owner_live
    from app.services.website.domain_resolution_service import canonical_hostname
    rows = (await db.execute(
        select(User, Site).join(AgentMarketArea, AgentMarketArea.owner_id == User.id)
        .outerjoin(Site, Site.owner_id == User.id)
        .where(AgentMarketArea.area_id == area.id).order_by(User.first_name, User.last_name)
    )).all()
    out = []
    for user, site in rows:
        live = bool(site and site.status == "PUBLISHED" and site.is_enabled is not False and _idx_owner_live(user))
        host = await canonical_hostname(db, site) if site else None
        out.append({
            "id": user.id, "name": f"{user.first_name or ''} {user.last_name or ''}".strip() or user.email,
            "email": user.email, "plan_tier": user.plan_tier,
            "site_host": host, "site_live": live,
            "page_url": f"https://{host}/{area.slug}" if host and live else None,
        })
    return out


def _entity_out(e: dict, shown: Optional[dict], category: str) -> dict:
    out = {k: e.get(k) for k in ("name", "category", "city", "address", "description", "source_url", "distance_miles",
                                  "lat", "lng", "google_checked", "survey_matched", "admin_edited", "admin_edited_at",
                                  "source_type", "removed_reason", "removed_at", "admin_removed")}
    shown = shown or {}
    meta = [medical_label(e.get("category")) if category == "medical" else e.get("category"), e.get("city")]
    d = shown.get("distance_miles")
    if isinstance(d, (int, float)):
        meta.append(f"{d:.1f} mi" if d < 1 else f"{d:.0f} mi")
    out["page"] = {"description": shown.get("description"), "meta": " · ".join(str(m) for m in meta if m), "distance_miles": d}
    return out


async def area_detail(db: AsyncSession, area: MarketArea) -> dict:
    facts = copy.deepcopy(area.facts or {})
    narrative = copy.deepcopy(area.narrative or {})
    page_facts, page_narrative = clean_area_payload(copy.deepcopy(facts), copy.deepcopy(narrative))
    lists = []
    for key, label, section in PLACE_LISTS:
        block = facts.get(key) if isinstance(facts.get(key), dict) else {}
        shown_block = page_facts.get(key) if isinstance(page_facts.get(key), dict) else {}
        shown = shown_block.get("entities") or []
        ents = [e for e in block.get("entities") or [] if isinstance(e, dict)]
        lists.append({
            "key": key, "label": label, "section": section,
            "notes": block.get("notes") or "", "page_notes": shown_block.get("notes") or "",
            "checked_at": block.get("_checked_at"), "admin_order": bool(block.get("_admin_order")),
            "page_limit": LIVING_PAGE_LIMIT if section == "Living here" else None,
            "entities": [_entity_out(e, shown[i] if i < len(shown) and isinstance(shown[i], dict) else None, key) for i, e in enumerate(ents)],
            "removed": [_entity_out(r, None, key) for r in block.get("_removed") or [] if isinstance(r, dict)],
        })
    ps = facts.get("public_schools") if isinstance(facts.get("public_schools"), dict) else {}
    schools = {
        "districts": [{"name": d.get("name"), "share": d.get("share"), "source_url": d.get("source_url")} for d in ps.get("districts") or [] if isinstance(d, dict)],
        "boundary_caveat": ps.get("boundary_caveat"),
        "levels": [{"key": k, "label": label, "schools": [{"name": s.get("name"), "district": s.get("district"), "admin_edited": s.get("admin_edited")}
                                                            for s in ps.get(k) or [] if isinstance(s, dict)]} for k, label in SCHOOL_LEVELS],
    }
    demo = page_facts.get("demographics") or {}
    pop = demo.get("population") if isinstance(demo.get("population"), dict) else {}
    stats = facts.get("market_stats") or {}
    other = [
        ["Population", f"{round(pop['value']):,}" + (f" ({pop['label']})" if pop.get("label") else "")] if pop.get("value") else None,
        ["Townships", ", ".join(f"{m.get('name')} {m.get('share')}%" for m in ((facts.get("boundaries") or {}).get("municipalities") or []) if isinstance(m, dict))] if (facts.get("boundaries") or {}).get("municipalities") else None,
        ["Homes in FEMA high-risk flood zones", f"{(facts.get('flood') or {}).get('high_risk_pct')}%"] if (facts.get("flood") or {}).get("homes_checked") else None,
        ["Getting around on foot", str((facts.get("walk") or {}).get("level"))] if (facts.get("walk") or {}).get("level") else None,
        ["Commute times", f"{len((facts.get('commute') or {}).get('destinations') or [])} places"] if (facts.get("commute") or {}).get("destinations") else None,
        ["Median list price (when researched)", f"${round(stats['median_price']):,}"] if stats.get("median_price") else None,
        ["Homes for sale (when researched)", str(stats.get("active_count"))] if stats.get("active_count") is not None else None,
    ]
    agents = await _agents_for(db, area)
    return {
        "id": area.id, "name": area.name, "state": area.state, "slug": area.slug, "county": area.county,
        "area_type": area.area_type, "parent_area_name": area.parent_area_name, "zip_codes": area.zip_codes or [],
        "enrichment_status": area.enrichment_status, "last_enriched_at": _iso(area.last_enriched_at),
        "created_at": _iso(area.created_at), "updated_at": _iso(area.updated_at),
        "places_check": {"version": facts.get("_places_check_version"), "checked_at": facts.get("_places_verified_at"),
                         "waiting": bool(facts.get("_places_check_waiting")), "running": area.id in _busy,
                         "lookups_estimate": _place_count(facts) + 2},
        "admin_edited_at": facts.get("_admin_edited_at"),
        "summary": {"text": narrative.get("summary") or "", "page_text": (page_narrative or {}).get("summary") or "",
                    "generated_at": narrative.get("generated_at"), "admin_edited": bool(narrative.get("admin_edited"))},
        "lists": lists,
        "public_schools": schools,
        "other_facts": [r for r in other if r],
        "agents": agents,
        "view_url": next((a["page_url"] for a in agents if a["page_url"]), None),
        "view_host": next((a["site_host"] for a in agents if a["page_url"]), None),
        "research_cost_usd": RESEARCH_COST_USD,
    }


async def _area_or_404(db: AsyncSession, area_id: str) -> MarketArea:
    area = await db.get(MarketArea, area_id)
    if not area:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That town isn't in the database any more.")
    return area


@router.get("/{area_id}")
async def get_area(area_id: str, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin_user)):
    return await area_detail(db, await _area_or_404(db, area_id))


# ---------------------------------------------------------------- edits (pure - see tests)

class PlaceIn(BaseModel):
    ref: Optional[int] = None          # its spot in the list as loaded (None = new or put back)
    removed_ref: Optional[int] = None  # put back: its spot in the "taken off" list as loaded
    name: str = ""
    category: Optional[str] = None
    city: Optional[str] = None
    address: Optional[str] = None
    description: Optional[str] = None
    source_url: Optional[str] = None


class PlacesSave(BaseModel):
    entities: list[PlaceIn] = Field(default_factory=list)
    notes: Optional[str] = None                 # None = leave the intro as it is
    base_names: list[str] = Field(default_factory=list)          # the list as the editor loaded it
    base_removed_names: list[str] = Field(default_factory=list)


class SchoolIn(BaseModel):
    ref: Optional[int] = None
    name: str = ""
    district: Optional[str] = None


class SchoolsSave(BaseModel):
    levels: dict[str, list[SchoolIn]] = Field(default_factory=dict)
    base: dict[str, list[str]] = Field(default_factory=dict)


class SummarySave(BaseModel):
    text: str = ""


_STALE = "This town changed since you opened it (a check or another edit). Reload it and make your change again."


def apply_place_edits(facts: dict, category: str, body: PlacesSave, now: str) -> dict:
    """The admin's version of one place list, written into a copy of `facts` (returned).
    Untouched places stay exactly as they were; changed, new and put-back ones are marked
    admin_edited; places left out go to the "taken off" list (admin_removed) so they can come back."""
    if category not in PLACE_KEYS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown list.")
    facts = copy.deepcopy(facts or {})
    block = dict(facts.get(category) or {}) if isinstance(facts.get(category), dict) else {}
    cur = [e for e in block.get("entities") or []]
    gone = [r for r in block.get("_removed") or []]
    if body.base_names != [e.get("name") if isinstance(e, dict) else None for e in cur] \
            or body.base_removed_names != [r.get("name") if isinstance(r, dict) else None for r in gone]:
        raise HTTPException(status.HTTP_409_CONFLICT, _STALE)
    used, back, out, refs = set(), set(), [], []
    for item in body.entities:
        fields = {f: _clean_field(f, getattr(item, f)) for f in EDIT_FIELDS}
        if not fields["name"]:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Every place needs a name.")
        if item.ref is not None:
            if not 0 <= item.ref < len(cur) or item.ref in used or not isinstance(cur[item.ref], dict):
                raise HTTPException(status.HTTP_409_CONFLICT, _STALE)
            used.add(item.ref)
            refs.append(item.ref)
            base, fresh = dict(cur[item.ref]), False
        elif item.removed_ref is not None:
            if not 0 <= item.removed_ref < len(gone) or item.removed_ref in back or not isinstance(gone[item.removed_ref], dict):
                raise HTTPException(status.HTTP_409_CONFLICT, _STALE)
            back.add(item.removed_ref)
            base = {k: v for k, v in gone[item.removed_ref].items() if k not in ("removed_reason", "removed_at", "admin_removed")}
            base["put_back_by_admin"] = True
            fresh = True
        else:
            base, fresh = {"relationship": "IN_AREA", "source_type": "admin", "added_by_admin": True}, True
        changed = fresh or any(_loose(base.get(f)) != v for f, v in fields.items())
        e = dict(base)
        for f, v in fields.items():
            if v is None:
                e.pop(f, None)
            else:
                e[f] = v
        if not fresh and _loose(base.get("address")) != fields["address"]:
            # A new address: the old map spot and driving distance belong to the old one.
            for k in ("lat", "lng", "distance_miles", "google_checked"):
                e.pop(k, None)
        if changed:
            e["admin_edited"] = True
            e["admin_edited_at"] = now
        out.append(e)
    for i, e in enumerate(cur):
        if i not in used and isinstance(e, dict):
            gone.append({**e, "removed_reason": "Taken off by the admin", "removed_at": now, "admin_removed": True})
    gone = [r for i, r in enumerate(gone) if i not in back]
    if refs != sorted(refs) or block.get("_admin_order"):
        block["_admin_order"] = [e["name"] for e in out]
    if body.notes is not None:
        notes = re.sub(r"[ \t]+", " ", body.notes).strip()
        if len(notes) > 2000:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Keep the intro under 2,000 characters.")
        if notes != (block.get("notes") or ""):
            if notes:
                block["notes"] = notes
            else:
                block.pop("notes", None)
            block["notes_admin_edited"] = True
    block["entities"] = out
    if gone:
        block["_removed"] = gone
    else:
        block.pop("_removed", None)
    facts[category] = block
    facts["_admin_edited_at"] = now
    return facts


def apply_school_edits(facts: dict, body: SchoolsSave, now: str) -> dict:
    """The admin's public schools by level, written to public_schools - and to the original research
    copy too when there is one, because the page rebuilds the list from it (reconcile_public_schools)."""
    facts = copy.deepcopy(facts or {})
    ps = dict(facts.get("public_schools") or {}) if isinstance(facts.get("public_schools"), dict) else {}
    for key, _label in SCHOOL_LEVELS:
        if key not in body.levels:
            continue
        cur = [s for s in ps.get(key) or []]
        if body.base.get(key, []) != [s.get("name") if isinstance(s, dict) else None for s in cur]:
            raise HTTPException(status.HTTP_409_CONFLICT, _STALE)
        out, used = [], set()
        for item in body.levels[key]:
            name = _clean_field("name", item.name)
            if not name:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Every school needs a name.")
            district = _clean_field("city", item.district) if item.district else None
            if item.ref is not None:
                if not 0 <= item.ref < len(cur) or item.ref in used or not isinstance(cur[item.ref], dict):
                    raise HTTPException(status.HTTP_409_CONFLICT, _STALE)
                used.add(item.ref)
                base = dict(cur[item.ref])
                changed = base.get("name") != name or (base.get("district") or None) != district
            else:
                base, changed = {"source_type": "admin", "added_by_admin": True}, True
            s = {**base, "name": name}
            if district:
                s["district"] = district
            else:
                s.pop("district", None)
            if changed:
                s.update(admin_edited=True, admin_edited_at=now)
            out.append(s)
        ps[key] = out
        research = facts.get("public_schools_research")
        if isinstance(research, dict):
            facts["public_schools_research"] = {**research, key: copy.deepcopy(out)}
    facts["public_schools"] = ps
    facts["_admin_edited_at"] = now
    return facts


def _texts(facts: dict, narrative: dict, category: Optional[str]) -> dict[str, str]:
    """The written text an edit can touch, by place - to tell the admin what the wording rules changed."""
    out = {}
    if category is None:
        out["Write-up"] = (narrative or {}).get("summary") or ""
        return out
    block = facts.get(category) or {}
    out["Intro"] = block.get("notes") or ""
    for e in block.get("entities") or []:
        if isinstance(e, dict) and e.get("admin_edited"):
            out[f"{e.get('name')} - description"] = e.get("description") or ""
    return out


def wording_changes(before: dict[str, str], after: dict[str, str]) -> list[dict]:
    return [{"where": k, "before": v, "after": after.get(k, "")} for k, v in before.items() if v and after.get(k, "") != v]


async def forget_cached_town(db: AsyncSession, area: MarketArea) -> None:
    """Drop every saved copy of this town's page data so the next visit is built from the record."""
    from app.services.website import content_adapters as ca
    ca._schools_cache.pop(area.id, None)
    owners = set((await db.execute(select(AgentMarketArea.owner_id).where(AgentMarketArea.area_id == area.id))).scalars().all())
    for key in list(ca._served_areas_cache):
        if key[0] in owners:  # town cards (the write-up's first lines) on those agents' sites
            ca._served_areas_cache.pop(key, None)
    for key in list(ca._market_cache):
        if key.startswith("related:") and key.split(":")[1] in owners:
            ca._market_cache.pop(key, None)


async def _save(db: AsyncSession, area: MarketArea, facts: dict, admin: User, category: Optional[str], narrative: Optional[dict] = None) -> dict:
    raw = _texts(facts, narrative if narrative is not None else (area.narrative or {}), category)
    area.facts = facts
    flag_modified(area, "facts")
    if narrative is not None:
        area.narrative = narrative
        flag_modified(area, "narrative")
    db.add(area)
    await fix_area_text(db, area, admin.id, use_ai=False)  # Fair Housing wording, fixed rules (commits)
    await db.refresh(area)
    await forget_cached_town(db, area)
    detail = await area_detail(db, area)
    detail["wording_changes"] = wording_changes(raw, _texts(area.facts or {}, area.narrative or {}, category))
    return detail


@router.put("/{area_id}/summary")
async def save_summary(area_id: str, body: SummarySave, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin_user)):
    area = await _area_or_404(db, area_id)
    text = re.sub(r"[ \t]+", " ", body.text or "").strip()
    if len(text) > 6000:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Keep the write-up under 6,000 characters.")
    now = _now()
    narrative = dict(area.narrative or {})
    narrative.update(summary=text, admin_edited=True, admin_edited_at=now)
    facts = dict(area.facts or {})
    facts["_admin_edited_at"] = now
    return await _save(db, area, facts, admin, None, narrative)


@router.put("/{area_id}/places/{category}")
async def save_places(area_id: str, category: str, body: PlacesSave, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin_user)):
    area = await _area_or_404(db, area_id)
    return await _save(db, area, apply_place_edits(area.facts or {}, category, body, _now()), admin, category)


@router.put("/{area_id}/public-schools")
async def save_public_schools(area_id: str, body: SchoolsSave, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin_user)):
    area = await _area_or_404(db, area_id)
    return await _save(db, area, apply_school_edits(area.facts or {}, body, _now()), admin, "public_schools")


# ---------------------------------------------------------------- actions

@router.post("/{area_id}/check-places")
async def check_places(area_id: str, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin_user)):
    """Checks this town's places on Google now (town_places_check) - uses Google's free monthly look-ups.
    Places the admin added or edited are left alone."""
    import os
    from app.services.areas.town_places_check import check_town_places
    area = await _area_or_404(db, area_id)
    if not os.getenv("GOOGLE_PLACES_API_KEY"):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The Google key isn't set on this server, so places can't be checked.")
    if area.centroid_lat is None or area.centroid_lng is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "This town has no map center yet - research it first.")
    if area.id in _busy:
        raise HTTPException(status.HTTP_409_CONFLICT, "This town is already being checked.")
    _busy.add(area.id)
    try:
        result = await asyncio.wait_for(check_town_places(db, area), timeout=240)
    except asyncio.TimeoutError:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "The check took too long - try again in a few minutes.")
    finally:
        _busy.discard(area.id)
    await db.refresh(area)
    await forget_cached_town(db, area)
    detail = await area_detail(db, area)
    detail["check_result"] = {"checked": result.get("checked", 0), "removed": result.get("removed", 0),
                              "removed_names": result.get("removed_names") or {}, "waiting": bool((area.facts or {}).get("_places_check_waiting"))}
    return detail


@router.post("/{area_id}/research")
async def research_again(area_id: str, background_tasks: BackgroundTasks, confirm_cost: bool = Query(False),
                         db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin_user)):
    """Runs the full town research again (about $0.66 in AI) - the same as POST /admin/areas/{id}/re-enrich.
    Asked for twice on purpose: the editor shows the cost and the admin confirms."""
    if not confirm_cost:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Research again costs about ${RESEARCH_COST_USD:.2f} - confirm the cost first.")
    from app.api.admin_routes import re_enrich_area
    await _area_or_404(db, area_id)
    await re_enrich_area(area_id, background_tasks, db, admin)
    area = await _area_or_404(db, area_id)
    return await area_detail(db, area)
