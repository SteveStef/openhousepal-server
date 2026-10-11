"""Guest Pass - where a guest's phone lands right after they sign in at an open house.

One page per guest (frontend app/open-house/[id]/pass/[token]): the home they're standing in, its
Similar Homes sheet and Cheat Sheet as live web pages, one-tap "request the seller disclosure" /
"request a second tour" (we already have their info from the sign-in), their Showcase, and - when
the host has one for this listing - a banner to its Spotlight.

The link carries a signed token for the sign-in (visitor) record, so only the guest who signed in
gets their own details pre-filled and can send requests as themselves. Everything here reuses the
existing flows (the Cheat Sheet request route, the Showcase builder, the listing page's tour /
message routes), so the lead activity, notifications and follow-ups are exactly the ones those
already make - a Guest Pass is a new way to get there, not a new situation.

Bright MLS plans only (Basic / Premium / IDX): it's built from MLS data. Showcase pieces only on
plans with Showcases. Switched per open house (OpenHouseEvent.guest_pass, on by default)."""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import (AgentHub, Collection, OpenHouseEvent, OpenHouseFormRequest,
                                 OpenHouseVisitor, Property, User)

logger = get_logger(__name__)

# A Guest Pass link keeps working this long after the sign-in (guests come back to it for days).
PASS_DAYS = 60
_NOT_FOUND = "This Guest Pass isn't available."
# Snapshot keys the Similar Homes page never shows - kept off the wire (the snapshot is a full copy
# of each listing).
_HEAVY_KEYS = {"raw_mls_data", "PublicRemarks", "public_remarks", "photos", "Photos", "Media", "description"}
# A similar home in one of these states is gone - it drops off the guest's list.
_GONE = ("CLOSED", "OFF_MARKET", "OFF MARKET", "WITHDRAWN", "EXPIRED", "CANCEL", "TEMP OFF")


async def _live_similar(db: AsyncSession, items: list[dict]) -> list[dict]:
    """The Similar Homes sheet is saved when the agent makes it; this brings each home up to date
    from the MLS feed (synced every few minutes) so the guest's list is live: today's price (a cut
    shows as a price drop), days on market counted from the list date, under-contract / coming-soon
    tags, and sold or pulled homes dropped. Homes not in our MLS table stay exactly as saved."""
    keys = [str(i.get("ListingKey")) for i in items if i.get("ListingKey")]
    if not keys:
        return items
    rows = {r.listing_key: r for r in (await db.execute(
        select(Property.listing_key, Property.price, Property.home_status,
               Property.raw_mls_data["MLSListDate"].astext.label("list_date"))
        .where(Property.listing_key.in_(keys))
    )).all()}
    today = datetime.now(timezone.utc).date()
    out = []
    for item in items:
        r = rows.get(str(item.get("ListingKey")))
        if r is None:
            out.append(item)
            continue
        status = (r.home_status or "").upper()
        if any(g in status for g in _GONE):
            continue
        item = dict(item)
        item["MlsStatus"] = r.home_status
        if "CONTRACT" in status or "PENDING" in status:
            item["LiveStatus"] = "Under contract"
        elif "COMING SOON" in status:
            item["LiveStatus"] = "Coming soon"
        try:
            saved = float(item.get("ListPrice") or 0)
        except (TypeError, ValueError):
            saved = 0
        if r.price:
            if saved and r.price < saved:
                item["PriceDrop"] = round(saved - r.price)
            item["ListPrice"] = r.price
        if r.list_date:
            try:
                listed = datetime.strptime(r.list_date[:10], "%Y-%m-%d").date()
                item["MLSListDate"] = listed.isoformat()
                item["DaysOnMarket"] = max(0, (today - listed).days)
            except ValueError:
                pass
        out.append(item)
    return out


def _sig(visitor_id: str) -> str:
    from app.utils.auth import JWT_SECRET_KEY
    return hmac.new(JWT_SECRET_KEY.encode(), f"guest-pass:{visitor_id}".encode(), hashlib.sha256).hexdigest()[:32]


def make_token(visitor_id: str) -> str:
    return f"{visitor_id}.{_sig(visitor_id)}"


def pass_path(visitor: OpenHouseVisitor) -> str:
    return f"/open-house/{visitor.open_house_event_id}/pass/{make_token(visitor.id)}"


async def pass_url(db: AsyncSession, host: User, visitor: OpenHouseVisitor) -> str:
    """The full Guest Pass link for anything that leaves the page (an email) - on the host's own
    domain when their links use it (IDX), same as their open-house sign-in QR."""
    from app.services.website.site_service import resolve_owner_public_base_url
    return f"{(await resolve_owner_public_base_url(db, host)).rstrip('/')}{pass_path(visitor)}"


async def _website_url(db: AsyncSession, host: User) -> Optional[str]:
    """The host's live IDX website, when they have one (IDX plan, published)."""
    if host.plan_tier != "IDX":
        return None
    from app.models.database import Site
    from app.services.website.site_service import resolve_owner_public_base_url
    site = (await db.execute(select(Site).where(Site.owner_id == host.id))).scalar_one_or_none()
    if not site or site.status != "PUBLISHED":
        return None
    return await resolve_owner_public_base_url(db, host)


async def resolve(db: AsyncSession, token: str) -> tuple[OpenHouseVisitor, OpenHouseEvent, User]:
    """(visitor, open house, host) for a valid, still-live Guest Pass - else 404."""
    from app.services.open_house_service import OpenHouseService
    visitor_id, _, sig = (token or "").partition(".")
    if not visitor_id or not sig or not hmac.compare_digest(sig, _sig(visitor_id)):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    visitor = await db.get(OpenHouseVisitor, visitor_id)
    if not visitor or not visitor.open_house_event_id:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    created = visitor.created_at
    if created and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if created and created < datetime.now(timezone.utc) - timedelta(days=PASS_DAYS):
        raise HTTPException(status_code=404, detail="This Guest Pass has expired.")
    oh = await db.get(OpenHouseEvent, visitor.open_house_event_id)
    host = await db.get(User, oh.agent_id) if oh and not oh.is_deleted else None
    if not OpenHouseService.guest_pass_on(oh, host):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return visitor, oh, host


def _first(*values):
    for v in values:
        if v not in (None, "", [], {}):
            return v
    return None


async def _agent(db: AsyncSession, host: User) -> dict:
    """The same agent block the Cheat Sheet prints (frontend lib/bragSheetData.ts
    buildCheatSheetAgentData): Agent Hub profile first, account details as the fallback."""
    from app.services.open_house_service import OpenHouseService
    hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == host.id))).scalar_one_or_none()
    config = (hub.config or {}) if hub else {}
    profile = config.get("profile") or {}
    contact = config.get("contact") or {}
    name = f"{host.first_name or ''} {host.last_name or ''}".strip()
    return {
        "id": host.id,
        "name": _first(profile.get("name"), name, "Your Agent"),
        "first_name": host.first_name or _first(profile.get("name"), name, "your agent").split(" ")[0],
        "title": _first(profile.get("title"), getattr(host, "professional_title", None)),
        "brokerage": _first(profile.get("brokerage"), host.brokerage),
        "photo_url": _first(config.get("profilePhotoUrl"), config.get("profile_photo_url"), host.photo_url),
        "photo_position": config.get("profilePhotoPosition") or config.get("photoPosition"),
        "phone": _first(contact.get("call"), contact.get("phone"), contact.get("mobile"), getattr(host, "phone", None)),
        "text_phone": _first(contact.get("text"), contact.get("call"), contact.get("phone"), contact.get("mobile"), getattr(host, "phone", None)),
        "email": _first(contact.get("email"), host.email),
        "hub_url": (await OpenHouseService.agent_hub_link(db, host))["url"],
        "website_url": await _website_url(db, host),
    }


async def _signature_slug(db: AsyncSession, host: User, oh) -> Optional[str]:
    """The host's Email Signature card (site version) closes the pass in place of the plain agent
    card: the open house's pass_signature switch is on (default), the plan has signatures and one
    is saved. None = show the regular agent card."""
    if getattr(oh, "pass_signature", True) is False:
        return None
    from app.services import email_signature_service
    if not await email_signature_service.has_signature_card(db, host):
        return None
    hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == host.id))).scalar_one_or_none()
    return hub.slug if hub and hub.slug else None


async def _showcase(db: AsyncSession, host: User, visitor: OpenHouseVisitor) -> Optional[Collection]:
    return (await db.execute(
        select(Collection).where(
            Collection.owner_id == host.id,
            func.lower(Collection.visitor_email) == (visitor.email or "").lower(),
            Collection.status == "ACTIVE",
        ).order_by(Collection.created_at.desc())
    )).scalars().first()


async def _showcase_url(db: AsyncSession, host: User, col: Collection) -> Optional[str]:
    if not col or not col.share_token:
        return None
    from app.services.website.site_service import resolve_owner_public_base_url
    return f"{await resolve_owner_public_base_url(db, host)}/showcase/{col.share_token}"


def showcases_offered(host: User, visitor: OpenHouseVisitor) -> bool:
    """The Showcase button: only for a guest without an agent, on a plan with Showcases."""
    from app.utils.auth import owner_has_access, SHOWCASE_ACCESS_TIERS
    return visitor.has_agent != "YES" and owner_has_access(host, SHOWCASE_ACCESS_TIERS)


async def payload(db: AsyncSession, visitor: OpenHouseVisitor, oh: OpenHouseEvent, host: User) -> dict:
    """Everything the Guest Pass page shows. Read live from the open house, so an edited Similar
    Homes sheet / Cheat Sheet shows up here the next time the page loads."""
    from app.services.open_house_service import OpenHouseService
    from app.services.visit_source import with_src
    photos: list = []
    if oh.listing_key:
        photos = (await db.execute(select(Property.photos).where(Property.listing_key == oh.listing_key))).scalar_one_or_none() or []
    if not photos and oh.cover_image_url:
        photos = [oh.cover_image_url]
    elif oh.cover_image_url and oh.cover_image_url in photos:
        photos = [oh.cover_image_url] + [p for p in photos if p != oh.cover_image_url]

    theme = {"accentColor": "#C9A24D"}
    hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == host.id))).scalar_one_or_none()
    if hub and (hub.config or {}).get("openHouseRequestTheme"):
        theme = {**theme, **(hub.config or {})["openHouseRequestTheme"]}
    theme["darkMode"] = bool(oh.qr_dark_mode)

    similar = await _live_similar(db, [{k: v for k, v in (item or {}).items() if k not in _HEAVY_KEYS}
                                       for item in (oh.similar_properties_snapshot or []) if isinstance(item, dict)])

    cheat = None
    snap = oh.cheat_sheet_snapshot or {}
    if isinstance(snap, dict) and snap.get("property"):
        cheat = {"property": {**snap["property"], "openHouseEventId": oh.id}, "amenities": snap.get("amenities")}

    spotlight = (await OpenHouseService.own_listing_spotlights(db, host, [oh])).get(oh.id)
    if spotlight:
        spotlight = f"{with_src(spotlight, 'pass')}&oh={oh.id}"

    showcase = None
    if showcases_offered(host, visitor):
        col = await _showcase(db, host, visitor)
        showcase = {"url": await _showcase_url(db, host, col)} if col else {"url": None}

    requested = {r for r in (await db.execute(
        select(OpenHouseFormRequest.form_type).where(
            OpenHouseFormRequest.open_house_event_id == oh.id,
            func.lower(OpenHouseFormRequest.email) == (visitor.email or "").lower(),
        )
    )).scalars()}

    return {
        "guest": {
            "first_name": (visitor.full_name or "").split(" ")[0],
            "full_name": visitor.full_name,
            "email": visitor.email,
            "phone": visitor.phone,
            "has_agent": visitor.has_agent == "YES",
        },
        "home": {
            "id": oh.id,
            "listing_key": oh.listing_key,
            "address": oh.address,
            "city": oh.city,
            "state": oh.state,
            "zipcode": oh.zipcode,
            "price": oh.price,
            "beds": oh.bedrooms,
            "baths": oh.bathrooms,
            "sqft": oh.living_area,
            "lot_size": oh.lot_size,
            "latitude": oh.latitude,
            "longitude": oh.longitude,
            "property_type": oh.house_type,
            "photos": photos[:24],
            "cover_position": oh.cover_image_position,
        },
        "agent": await _agent(db, host),
        "signature_hub_slug": await _signature_slug(db, host, oh),
        "theme": theme,
        "similar": similar,
        "live_at": datetime.now(timezone.utc).isoformat(),
        "cheat_sheet": cheat,
        "spotlight_url": spotlight,
        "showcase": showcase,
        "requested": {"disclosure": "disclosure" in requested, "second_visit": "second_visit" in requested},
    }


async def send_request(db: AsyncSession, visitor: OpenHouseVisitor, oh: OpenHouseEvent, kind: str, preferred_time: str | None = None) -> dict:
    """One tap = the Cheat Sheet's disclosure / second-visit form, filled in with what they gave at
    sign-in, sent through the very same route (same lead activity, notification and follow-up). A
    repeat tap doesn't send it twice."""
    from app.api.open_houses_routes import submit_open_house_form_request
    from app.schemas.open_house import OpenHouseFormRequestCreate, OpenHouseFormRequestType
    existing = (await db.execute(
        select(OpenHouseFormRequest.id).where(
            OpenHouseFormRequest.open_house_event_id == oh.id,
            OpenHouseFormRequest.form_type == kind,
            func.lower(OpenHouseFormRequest.email) == (visitor.email or "").lower(),
        )
    )).first()
    if existing:
        return {"sent": True, "already": True}
    data = OpenHouseFormRequestCreate(
        form_type=OpenHouseFormRequestType(kind),
        full_name=visitor.full_name,
        email=visitor.email,
        phone=visitor.phone or "",
        # A second tour carries the two times they picked - shown on the request + lead activity.
        preferred_visit_time=((preferred_time or "").strip()[:200] or None) if kind == "second_visit" else None,
    )
    await submit_open_house_form_request(oh.id, data, db)
    return {"sent": True, "already": False}


async def ask_question(db: AsyncSession, visitor: OpenHouseVisitor, oh: OpenHouseEvent, host: User, message: str) -> dict:
    """"Ask {agent} a quick question" - the listing page's own "Message agent" route with their
    sign-in info, so it's the same lead activity, bell alert, agent email and "listing inquiry"
    follow-up as any question sent from a home's page."""
    from app.api.properties_routes import AgentMessageRequest, message_agent
    text = (message or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="Type your question first.")
    address = ", ".join(p for p in [oh.address, oh.city, oh.state] if p)
    await message_agent(AgentMessageRequest(
        agent_id=host.id, property_id=oh.listing_key or oh.id, property_address=address or "your open house",
        visitor_name=visitor.full_name or "Open house guest", visitor_contact=visitor.email or visitor.phone or "",
        message=text[:2000], came_from={"src": "pass", "oh": oh.id},
    ), db)
    await _note_on_open_followup(db, host, visitor, text[:2000])
    return {"sent": True}


async def _note_on_open_followup(db: AsyncSession, host: User, visitor: OpenHouseVisitor, text: str) -> None:
    """A guest usually already has an open follow-up from signing in (one per person at a time),
    so the question doesn't make a second one - it goes onto that one, and its calendar event is
    re-pushed so the agent sees the question when the reminder goes off."""
    try:
        import asyncio
        from app.models.database import AgentTask, Lead
        from app.services.calendar_sync_service import sync_task_bg
        await asyncio.sleep(0.5)  # the inquiry's own follow-up check runs in the background first
        lead = (await db.execute(select(Lead).where(Lead.owner_id == host.id, func.lower(Lead.email) == (visitor.email or "").lower())
                                 .order_by(Lead.created_at.desc()))).scalars().first()
        if lead is None:
            return
        task = (await db.execute(select(AgentTask).where(
            AgentTask.owner_id == host.id, AgentTask.lead_id == lead.id,
            AgentTask.auto_source.is_not(None), AgentTask.completed_at.is_(None),
        ).order_by(AgentTask.created_at.desc()))).scalars().first()
        if task is None or text in (task.notes or ""):
            return
        line = f'Asked on their Guest Pass: "{text}"'
        task.notes = f"{task.notes}\n{line}" if task.notes else line
        db.add(task)
        await db.commit()
        sync_task_bg(host.id, task.id)
    except Exception:  # noqa: BLE001 - the question already went through
        logger.warning("guest pass question: follow-up note failed", exc_info=True)


async def open_showcase(db: AsyncSession, visitor: OpenHouseVisitor, oh: OpenHouseEvent, host: User) -> dict:
    """Their Showcase link - building it now, the same way the sign-in would have (homes like this
    one + the "your homes are ready" email an hour out), when it wasn't made at sign-in (the
    open house's Showcases switch was off, or the build didn't go through)."""
    from app.schemas.open_house import HasAgent, OpenHouseFormSubmission
    from app.services.open_house_service import OpenHouseService
    if not showcases_offered(host, visitor):
        raise HTTPException(status_code=404, detail="Showcases aren't available here.")
    col = await _showcase(db, host, visitor)
    if not col:
        form = OpenHouseFormSubmission(
            full_name=visitor.full_name, email=visitor.email, phone=visitor.phone or "",
            has_agent=HasAgent(visitor.has_agent) if visitor.has_agent in HasAgent._value2member_map_ else HasAgent.NO,
            open_house_event_id=oh.id,
        )
        result = await OpenHouseService.create_showcase_and_invite(db, visitor, form, send_after=timedelta(hours=1))
        if not result.get("success"):
            raise HTTPException(status_code=409, detail="We couldn't build your Showcase right now.")
        if visitor.lead_id:
            from app.services.tasks_service import resync_lead_followups
            resync_lead_followups(host.id, visitor.lead_id, delay=20)
        col = await _showcase(db, host, visitor)
    url = await _showcase_url(db, host, col)
    if not url:
        raise HTTPException(status_code=409, detail="We couldn't build your Showcase right now.")
    return {"url": url}
