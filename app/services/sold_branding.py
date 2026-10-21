"""Sold-home branding rule (owner, 10/21).

A SOLD (closed) home may carry an OpenHousePal agent's branding - their name, photo, brokerage,
contact buttons, signature card, "presented by" lines, their share-card pill - only when that agent
actually took part in the sale: the listing agent, co-listing agent, buyer's agent or co-buyer's
agent on that closed sale. Anyone else showing a sold home (an IDX site's search result, a
Showcase home that sold to someone else, a shared Listing Page link...) shows it neutral, so
nobody reads it as "this agent sold it". Active / pending / coming-soon homes are unchanged.

The one place this is decided - every public surface asks `branding_allowed_for_listing` (or
`sold_branding_allowed` when it already holds the Property row) and the frontend only reads the
flag it is sent. CMA reports are the agent's own report and deliberately do not use this.

Who counts, from the Bright record (properties.raw_mls_data) plus our own columns:
    ListAgentMlsId, CoListAgentMlsId, BuyerAgentMlsId, CoBuyerAgentMlsId,
    properties.list_agent_mls_id, properties.co_list_agent_mls_id
matched against the agent's own User.mls_id (their Bright MLS ID). An agent with no MLS ID never
matches a Bright sale -> neutral. The one exception is a home they hand-entered themselves
(is_manual rows store "user:<id>" as the list agent, see utils/plan_ids.listing_owner_key) - that
is their own listing, so it counts as them being the listing agent.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# The Bright fields that name an agent on a closed sale.
SALE_AGENT_MLS_FIELDS = ("ListAgentMlsId", "CoListAgentMlsId", "BuyerAgentMlsId", "CoBuyerAgentMlsId")


def _field(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _norm(value: Any) -> Optional[str]:
    s = str(value).strip() if value is not None else ""
    return s.upper() or None


def is_sold_status(*statuses: Any) -> bool:
    """Bright MlsStatus "CLOSED-BRIGHT", StandardStatus "Closed", a hand-entered "Sold"/"CLOSED"."""
    for st in statuses:
        up = str(st or "").strip().upper()
        if not up:
            continue
        if "CLOSED" in up or up == "SOLD" or up.startswith("SOLD"):
            return True
    return False


def is_sold(prop: Any) -> bool:
    """`prop` is a Property row or a dict with the same field names (home_status, raw_mls_data)."""
    raw = _field(prop, "raw_mls_data") or {}
    if not isinstance(raw, dict):
        raw = {}
    return is_sold_status(_field(prop, "home_status"), raw.get("MlsStatus"), raw.get("StandardStatus"))


def sale_agent_ids(prop: Any) -> set[str]:
    """Every agent ID the record names on the sale (listing side + buyer side), normalized."""
    raw = _field(prop, "raw_mls_data") or {}
    if not isinstance(raw, dict):
        raw = {}
    values: Iterable[Any] = [raw.get(k) for k in SALE_AGENT_MLS_FIELDS] + [
        _field(prop, "list_agent_mls_id"), _field(prop, "co_list_agent_mls_id"),
    ]
    return {v for v in (_norm(x) for x in values) if v}


def agent_ids(agent: Any) -> set[str]:
    """The IDs that are this agent on a listing record: their Bright MLS ID, and "user:<id>" for the
    homes they hand-entered themselves (never equal to a real Bright MLS ID)."""
    out: set[str] = set()
    if agent is None:
        return out
    mls = _norm(_field(agent, "mls_id"))
    if mls:
        out.add(mls)
    uid = _field(agent, "id")
    if uid:
        out.add(f"USER:{str(uid).strip().upper()}")
    return out


def agent_took_part_in_sale(agent: Any, prop: Any) -> bool:
    """True when the agent is the listing / co-listing / buyer's / co-buyer's agent on this record."""
    if agent is None or prop is None:
        return False
    return bool(agent_ids(agent) & sale_agent_ids(prop))


def sold_branding_allowed(agent: Any, prop: Any) -> bool:
    """May this home be shown with this agent's branding? Not sold -> yes (unchanged). Sold -> only
    when they took part in the sale. No record at all -> yes (nothing says it sold)."""
    if prop is None or not is_sold(prop):
        return True
    return agent_took_part_in_sale(agent, prop)


async def branding_allowed_for_listing(db: AsyncSession, agent: Any, listing_key: Optional[str]) -> bool:
    """`sold_branding_allowed` for a listing key, read from the MLS mirror in one small query."""
    if not listing_key:
        return True
    from app.models.database import Property
    # Only the few JSON keys the rule reads - not the whole Bright record.
    raw_keys = SALE_AGENT_MLS_FIELDS + ("MlsStatus", "StandardStatus")
    row = (await db.execute(
        select(Property.home_status, Property.list_agent_mls_id, Property.co_list_agent_mls_id,
               *[Property.raw_mls_data[k].astext for k in raw_keys])
        .where(Property.listing_key == str(listing_key))
    )).first()
    if row is None:
        return True
    return sold_branding_allowed(agent, {
        "home_status": row[0], "list_agent_mls_id": row[1], "co_list_agent_mls_id": row[2],
        "raw_mls_data": {k: v for k, v in zip(raw_keys, row[3:]) if v is not None},
    })
