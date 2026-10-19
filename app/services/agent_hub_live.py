"""Agent Hub listings + recent sales that keep themselves current.

The Agent Hub stores the agent's curated `myListings` / `recentSales` in AgentHub.config (their
show/hide choices and any hand-added entries). Left alone, that saved list goes stale the moment
the agent lists a new home or closes a sale. `live_hub_config` merges the synced MLS mirror into it
every time the hub is read (public page, the agent's own editor, the email signature):

  * every current listing (Coming Soon / Active / Pending / Under Contract) where the agent is the
    list or co-list agent - new ones are added (shown) automatically, existing ones get today's
    status, price and photos, and ones that have left the market drop off;
  * every new closed sale under their MLS id is added to Recent Sales (shown);
  * hand-added (manual) entries and the agent's own show/hide choices are always kept.

Reads only the local `properties` mirror - never calls Bright.
"""
import logging
import re
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Property, User
from app.services.mls_open_houses import open_houses_payload

logger = logging.getLogger(__name__)

INACTIVE_STATUSES = {"CLOSED-BRIGHT", "EXPIRED-BRIGHT", "WITHDRAWN-BRIGHT", "CANCELED-BRIGHT", "OFF_MARKET-BRIGHT"}


def nice(text: Optional[str]) -> str:
    """MLS text arrives in ALL CAPS ("239 WINDHAM DR") - show it as "239 Windham Dr". Mixed-case
    text the agent typed is left exactly as is."""
    t = (text or "").strip()
    if not t or t != t.upper():
        return t
    return re.sub(r"[A-Za-z]+('[A-Za-z]+)?", lambda m: m.group(0).capitalize(), t.lower())


def _addr_key(address: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", str(address or "").split(",")[0].lower())


def _listing_item(p: Property) -> dict:
    photos = p.photos if isinstance(p.photos, list) and p.photos else ([p.img_src] if p.img_src else [])
    return {
        "id": p.listing_key,
        "address": nice(p.street_address or p.full_address) or "Address unavailable",
        "city": nice(p.city),
        "state": p.state or "",
        "zipcode": p.zipcode or None,
        "price": p.price or None,
        "status": p.home_status or "Active",
        "image": p.img_src or (photos[0] if photos else None),
        "photos": photos[:8] or None,
        "source": "listing",
        "beds": p.bedrooms or None,
        "baths": p.bathrooms or None,
        "sqft": p.living_area or None,
        "acres": p.lot_size_acres or None,
        # Upcoming Bright open houses, soonest first - the hub cards' "Open House" chip. Rebuilt
        # on every read, so a saved copy in the config never goes stale.
        "open_houses": open_houses_payload(p),
    }


async def live_listings(db: AsyncSession, owner: User) -> list[Property]:
    """The agent's own on-market listings in the mirror, newest first."""
    if not owner.mls_id:
        return []
    mls_id = owner.mls_id.strip()
    result = await db.execute(
        select(Property).where(
            or_(Property.list_agent_mls_id == mls_id, Property.co_list_agent_mls_id == mls_id),
            Property.home_status.notin_(INACTIVE_STATUSES),
        ).order_by(Property.mls_list_date.desc().nullslast(), Property.created_at.desc().nullslast())
    )
    return list(result.scalars().all())


async def _merged_listings(db: AsyncSession, owner: User, saved: list) -> list:
    saved = [s for s in saved if isinstance(s, dict)]
    live = await live_listings(db, owner)
    if not owner.mls_id:
        return saved
    prev_by_id = {str(s.get("id")): s for s in saved if s.get("source") != "manual"}
    manual = [s for s in saved if s.get("source") == "manual"]
    live_ids = {p.listing_key for p in live}
    fresh = []
    for p in live:
        item = _listing_item(p)
        prev = prev_by_id.get(str(p.listing_key))
        # Their show/hide choice sticks; a brand-new listing shows by default.
        item["visible"] = prev.get("visible", True) if prev else True
        item["_new"] = prev is None
        fresh.append(item)
    # Keep the agent's saved order for listings they already had; new ones lead (newest first).
    order = {str(s.get("id")): i for i, s in enumerate(saved)}
    new_items = [i for i in fresh if i.pop("_new")]
    known = sorted([i for i in fresh if str(i["id"]) in order], key=lambda i: order[str(i["id"])])
    # A manual entry for the same address as a now-synced MLS listing is the same home - the MLS
    # version (with live status) wins.
    live_addrs = {_addr_key(i["address"]) for i in fresh}
    manual = [m for m in manual if _addr_key(m.get("address")) not in live_addrs or m.get("id") in live_ids]
    return new_items + known + manual


async def _merged_sales(db: AsyncSession, owner: User, saved: list) -> list:
    from app.services.website.content_adapters import get_recent_sales
    saved = [s for s in saved if isinstance(s, dict)]
    if not owner.mls_id:
        return saved
    closed = await get_recent_sales(db, owner, limit=24)
    seen_ids = {str(s.get("id")) for s in saved}
    seen_addrs = {_addr_key(s.get("address")) for s in saved}
    added = []
    for sale in closed:
        key = str(sale.get("listing_key") or sale.get("id"))
        if key in seen_ids or _addr_key(sale.get("address")) in seen_addrs:
            continue
        sold = sale.get("sold_date")
        added.append({
            "id": key,
            "address": nice((sale.get("address") or "").split(",")[0]) or "Address unavailable",
            "city": nice(sale.get("city")),
            "state": sale.get("state") or "",
            "price": sale.get("price"),
            "status": sale.get("status") or "Closed",
            "image": sale.get("photo"),
            "sold_date": sold.isoformat() if hasattr(sold, "isoformat") else sold,
            "beds": sale.get("bedrooms"),
            "baths": sale.get("bathrooms"),
            "sqft": sale.get("living_area"),
            "source": "bright",
            "visible": True,
        })
    return added + saved


async def live_hub_config(db: AsyncSession, owner: Optional[User], config: Optional[dict]) -> dict:
    """The hub config with today's listings + sales merged in (see module docstring). Never raises -
    on any problem it returns the saved config unchanged."""
    cfg = dict(config or {})
    if owner is None:
        return cfg
    try:
        cfg["myListings"] = await _merged_listings(db, owner, cfg.get("myListings") or [])
        cfg["recentSales"] = await _merged_sales(db, owner, cfg.get("recentSales") or [])
    except Exception:
        logger.warning("live Agent Hub merge failed - serving saved lists", exc_info=True)
        cfg = dict(config or {})
    # The Website link fills itself in: IDX site / own domain, else the hub (website_link_service).
    from app.services.website_link_service import apply_auto_website
    return await apply_auto_website(db, owner, cfg)
