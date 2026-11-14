"""The chat's Home search card (owner, 11/08): "find homes for sale in Exton with a first-floor primary
bedroom" never reads a whole town by itself any more. The chat shows a search card with the boxes
filled from the question; the agent fills the required ones - a town / zip or a school district, the
least bedrooms and the least house size - and sees, as they type, how many homes fit and how much of
their chat usage reading them takes. Counting is a plain database count: free, no AI. Search only
opens at READ_CAP homes or fewer and only when their chat usage has room for it; then the AI reads
each listing (ai_home_reader) and the card shows the matches with the words that show it.

The results are saved on the chat message's card, so they're there when the chat is opened again and
the next question ("add those to Nicole's Showcase") knows which homes they were."""
from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Property, User
from app.services import mls_mirror
from app.services.ai import ai_home_reader as reader
from app.services.email_service import tidy_caps

FOR_SALE = ("ACTIVE-BRIGHT", "COMING SOON-BRIGHT", "ACTIVE", "COMING SOON")
SIZES = (1000, 1500, 2000, 2500, 3000, 3500, 4000, 5000)
_STATE = re.compile(r",?\s+(pa|pennsylvania|nj|new jersey|de|delaware|md|maryland|va|virginia|dc|wv|ny)\.?$", re.I)
_DISTRICT_FILLER = re.compile(r"\b(the|of|school|schools|district|sd|public|area\s+school)\b", re.I)


def _num(v) -> Optional[float]:
    try:
        n = float(str(v).replace(",", "").replace("$", "").strip())
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def clean(f: Dict[str, Any]) -> Dict[str, Any]:
    """The card's boxes, tidied: {town, district, min_beds, min_sqft, min_price, max_price, question}."""
    f = f or {}
    town = _STATE.sub("", str(f.get("town") or "").strip()).strip(" ,")
    out = {
        "town": town[:80],
        "district": str(f.get("district") or "").strip()[:120],
        "min_beds": int(_num(f.get("min_beds")) or 0) or None,
        "min_sqft": int(_num(f.get("min_sqft")) or 0) or None,
        "min_price": _num(f.get("min_price")),
        "max_price": _num(f.get("max_price")),
        "question": str(f.get("question") or "").strip()[:400],
    }
    return out


def missing(f: Dict[str, Any]) -> List[str]:
    out = []
    if not f.get("town") and not f.get("district"):
        out.append("place")
    if not f.get("min_beds"):
        out.append("min_beds")
    if not f.get("min_sqft"):
        out.append("min_sqft")
    if not f.get("question"):
        out.append("question")
    return out


def _where(f: Dict[str, Any]) -> list:
    lease = mls_mirror.col(Property.mls_property_type, "PropertyType")
    w = [Property.home_status.in_(FOR_SALE), or_(lease.is_(None), ~lease.ilike("%lease%"))]
    town = f.get("town")
    if town:
        if re.fullmatch(r"\d{5}", town):
            w.append(Property.zipcode.like(f"{town}%"))
        else:
            w.append(or_(Property.city.ilike(_esc(town)), Property.township.ilike(_esc(town))))
    if f.get("district"):
        w.append(Property.school_district_name.ilike(_esc(f["district"])))
    if f.get("min_beds"):
        w.append(Property.bedrooms >= f["min_beds"])
    if f.get("min_sqft"):
        w.append(Property.living_area >= f["min_sqft"])
    if f.get("min_price"):
        w.append(Property.price >= f["min_price"])
    if f.get("max_price"):
        w.append(Property.price <= f["max_price"])
    return w


async def resolve_district(db: AsyncSession, text: str) -> Optional[str]:
    """'West Chester Area School District' -> the MLS's own name for it ('WEST CHESTER AREA'), the
    one with the most homes for sale when several fit. None when nothing fits."""
    text = (text or "").strip()
    if not text:
        return None
    exact = (await db.execute(select(Property.school_district_name).where(
        Property.school_district_name.ilike(_esc(text)), Property.home_status.in_(FOR_SALE)).limit(1))).scalar()
    if exact:
        return exact
    core = re.sub(r"\s+", " ", _DISTRICT_FILLER.sub(" ", text)).strip()
    if len(core) < 3:
        return None
    row = (await db.execute(select(Property.school_district_name, func.count()).where(
        Property.school_district_name.ilike(f"%{_esc(core)}%"), Property.home_status.in_(FOR_SALE))
        .group_by(Property.school_district_name).order_by(func.count().desc()).limit(1))).first()
    return row[0] if row else None


async def places(db: AsyncSession, q: str) -> List[Dict[str, Any]]:
    """Towns, zips and school districts that start with what was typed, with how many homes are for
    sale in each - for the card's place box. Free (no AI)."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    base = [Property.home_status.in_(FOR_SALE)]
    out: List[Dict[str, Any]] = []
    if re.fullmatch(r"\d{2,5}", q):
        rows = (await db.execute(select(func.left(Property.zipcode, 5), func.count()).where(*base, Property.zipcode.like(f"{q}%"))
                                 .group_by(func.left(Property.zipcode, 5)).order_by(func.count().desc()).limit(6))).all()
        return [{"kind": "town", "value": z, "label": z, "count": n} for z, n in rows if z]
    like = f"{_esc(q)}%"
    towns = (await db.execute(select(Property.city, Property.state, func.count()).where(*base, Property.city.ilike(like))
                              .group_by(Property.city, Property.state).order_by(func.count().desc()).limit(5))).all()
    out += [{"kind": "town", "value": tidy_caps(c), "label": f"{tidy_caps(c)}, {s}" if s else tidy_caps(c), "count": n} for c, s, n in towns if c]
    dists = (await db.execute(select(Property.school_district_name, func.count()).where(
        *base, or_(Property.school_district_name.ilike(like), Property.school_district_name.ilike(f"% {_esc(q)}%")))
        .group_by(Property.school_district_name).order_by(func.count().desc()).limit(4))).all()
    out += [{"kind": "district", "value": d, "label": f"{tidy_caps(d)} school district", "count": n} for d, n in dists if d]
    return out


async def count(db: AsyncSession, user: User, config: dict, f: Dict[str, Any]) -> Dict[str, Any]:
    """How many homes fit and what reading them would take - a database count, no AI."""
    f = clean(f)
    need = missing(f)
    place_ok = bool(f.get("town") or f.get("district"))
    n, chars = 0, 0
    if place_ok:
        n, chars = (await db.execute(select(func.count(), func.coalesce(func.sum(func.least(func.length(func.coalesce(Property.description, "")), 1800)), 0))
                                     .select_from(Property).where(*_where(f)))).one()
        n, chars = int(n or 0), int(chars or 0)
    left, limit = await reader.room(db, user, config)
    # Each home's facts + MLS features ride along with its description (~1,300 characters).
    est = reader.estimate_usd(config, chars + n * 1300, min(n, reader.READ_CAP)) * (min(1.0, reader.READ_CAP / n) if n > reader.READ_CAP else 1)
    pct = reader.percent(est, limit)
    left_pct = None if left is None else reader.percent(left, limit)
    enough = left is None or est <= left
    ok = not need and 0 < n <= reader.READ_CAP and enough
    return {"count": n, "cap": reader.READ_CAP, "missing": need, "usage_percent": pct, "left_percent": left_pct,
            "enough": enough, "ready": ok, "filters": f}


async def run(db: AsyncSession, user: User, config: dict, f: Dict[str, Any]) -> Dict[str, Any]:
    """Read every home that fits (at most READ_CAP) with AI. The caller already checked count()."""
    f = clean(f)
    homes = list((await db.execute(select(Property).where(*_where(f)).order_by(Property.mls_list_date.desc().nullslast())
                                   .limit(reader.READ_CAP))).scalars())
    got = await reader.read(config, f["question"], homes)
    rows = []
    for p in homes:
        v = got["verdicts"].get(p.id)
        if not v:
            continue
        rows.append({"id": p.id, "key": p.listing_key, "street": tidy_caps(p.street_address or p.full_address or ""), "city": tidy_caps(p.city or ""),
                     "state": p.state, "price": p.price, "beds": p.bedrooms, "baths": p.bathrooms, "sqft": p.living_area, "photo": p.img_src,
                     "v": v["v"], "quote": v.get("quote") or "", "src": v.get("src") or "", "why": v.get("why") or ""})
    rows.sort(key=lambda r: 0 if r["v"] == "yes" else 1)
    return {"filters": f, "read": len(got["read"]), "failed": len(got["failed"]), "homes": rows,
            "yes": sum(1 for r in rows if r["v"] == "yes"), "maybe": sum(1 for r in rows if r["v"] == "maybe"),
            "_cost": {"usd": got["usd"], "in": got["in"], "out": got["out"]}}


async def card_for(db: AsyncSession, user: User, config: dict, args: Dict[str, Any]) -> Dict[str, Any]:
    """The chat tool: the Home search card, filled from the question (nothing read yet)."""
    lf = args.get("look_for")
    f = clean({**args, "question": args.get("question") or (" / ".join(str(x) for x in lf) if isinstance(lf, list) else lf) or ""})
    if f["district"]:
        f["district"] = await resolve_district(db, f["district"]) or f["district"]
    info = await count(db, user, config, f)
    return {"tool": "search_homes_for_sale", "record_type": "home_search", "record_id": str(uuid.uuid4()),
            "filters": info["filters"], "count": info["count"], "result": None}
