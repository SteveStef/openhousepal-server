"""Showcase smart filter (owner, 11/09): "9 ft+ ceilings", "pool", "first-floor primary" - up to 3 things the
agent wants on top of a Showcase's regular search. The regular search still decides which homes are in the
Showcase; the AI listing reader (services/ai/ai_home_reader.py - the chat's own, with its one-level-home and
"at least" rules) reads each home once for each thing, and a home that has ALL of them is starred, as if the
agent had recommended it, with a line under its address: "Appears to have: first-floor primary bedroom".
The other homes stay, without the star or the line.

- Runs after the agent saves the search or the must-haves, and when a new listing lands in the Showcase.
  Each home is read once per set of must-haves (collection_properties.ai_match.k) - a new listing costs
  one home's read, not the whole Showcase's.
- Costs the agent's chat usage, like the chat's own listing reads: checked against what's left before
  anything runs (none left: nothing runs and the Showcase says so), held against the platform-wide AI
  ceilings while it runs, then recorded.
- Starred once: if the agent takes the star off, the next read never puts it back (ai_match.starred).
- Clients never set it (it spends the agent's usage): only the agent's own route saves ai_wants.

collection_preferences.ai_wants = {"wants": [...], "state": "reading"|"done"|"no_room"|"error"|"off",
                                   "read": n, "matched": n, "left": n, "at": iso}
"""
import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, select, text

logger = logging.getLogger(__name__)

MAX_WANTS = 3
MAX_WANT_CHARS = 60
_running: set = set()       # collection ids reading right now (one run at a time per Showcase)
_again: set = set()         # asked again while running - one more pass when it ends


def clean_wants(raw: Any) -> List[str]:
    """The agent's must-haves: trimmed, no repeats, at most 3 of at most 60 characters."""
    if isinstance(raw, dict):
        raw = raw.get("wants")
    if isinstance(raw, str):
        raw = re.split(r"[,\n;]+", raw)
    out: List[str] = []
    for w in raw or []:
        w = " ".join(str(w or "").split())[:MAX_WANT_CHARS].strip(" .-")
        if w and w.lower() not in (x.lower() for x in out):
            out.append(w)
    return out[:MAX_WANTS]


def wants_key(wants: List[str]) -> str:
    return "|".join(sorted(w.lower() for w in wants))


def state_of(prefs) -> Dict[str, Any]:
    v = getattr(prefs, "ai_wants", None)
    return dict(v) if isinstance(v, dict) else {}


def _all_yes(verdicts: List[Optional[dict]]) -> str:
    if any(v is None for v in verdicts):
        return "no"
    if all(v.get("v") == "yes" for v in verdicts):
        return "yes"
    return "maybe"


async def _set_state(db, prefs_id: str, st: Dict[str, Any]) -> None:
    await db.execute(text("update collection_preferences set ai_wants = cast(:v as jsonb) where id = :i"), {"v": json.dumps(st), "i": prefs_id})
    await db.commit()


async def run_for(collection_id: str) -> Dict[str, Any]:
    """Read the Showcase's homes that haven't been read for its current must-haves. Own session - safe in
    the background. Returns the state it left."""
    from app.database import AsyncSessionLocal
    from app.models.database import Collection, CollectionPreferences, Property, PropertyInteraction, User, collection_properties
    from app.services.ai import ai_home_reader as reader
    from app.services.ai.ai_config_service import ai_config_service
    from app.services.ai.ai_rate_limit_service import record_usage_event, reconcile_budget, reserve_global_and_emergency, AIRateLimitExceeded

    async with AsyncSessionLocal() as db:
        coll = await db.get(Collection, collection_id)
        prefs = (await db.execute(select(CollectionPreferences).where(CollectionPreferences.collection_id == collection_id))).scalar_one_or_none()
        if coll is None or prefs is None:
            return {}
        st = state_of(prefs)
        wants = clean_wants(st.get("wants"))
        if not wants:
            return st
        user = await db.get(User, coll.owner_id)
        from app.utils.auth import tier_has_mls
        if user is None or not (tier_has_mls(user.plan_tier) or user.is_admin):
            return st
        k = wants_key(wants)
        # The homes for sale in it that this set of must-haves hasn't read yet.
        rows = (await db.execute(
            select(Property, collection_properties.c.ai_match)
            .join(collection_properties, collection_properties.c.property_id == Property.id)
            .where(collection_properties.c.collection_id == collection_id,
                   collection_properties.c.sold_filed_at.is_(None),
                   Property.home_status.in_(("ACTIVE-BRIGHT", "COMING SOON-BRIGHT", "ACTIVE", "COMING SOON")))
            .order_by(collection_properties.c.added_at.desc().nullslast())
        )).all()
        removed = {r[0] for r in (await db.execute(select(PropertyInteraction.property_id).where(and_(
            PropertyInteraction.collection_id == collection_id, PropertyInteraction.agent_removed_at.is_not(None))))).all()}
        todo = [(p, old) for p, old in rows if p.id not in removed and not (isinstance(old, dict) and old.get("k") == k)]
        if len(todo) > reader.READ_CAP:
            # Same ceiling as the chat's own listing reads (owner, 11/09): more than 300 homes to read never
            # starts - the agent narrows the search first. New listings arriving one by one never hit this.
            out = {**st, "wants": wants, "state": "too_many", "left": len(todo), "cap": reader.READ_CAP,
                   "at": datetime.now(timezone.utc).isoformat()}
            await _set_state(db, prefs.id, out)
            return out
        if not todo:
            done = {**st, "wants": wants, "state": "done", "left": 0, "at": datetime.now(timezone.utc).isoformat(),
                    "matched": await _count_matched(db, collection_id, k)}
            await _set_state(db, prefs.id, done)
            return done

        config = await ai_config_service.get_config(db)
        homes = [p for p, _ in todo]
        est = reader.estimate_for(config, homes) * len(wants)
        left, limit = await reader.room(db, user, config)
        if left is not None and est > left:
            out = {**st, "wants": wants, "state": "no_room", "left": len(todo), "at": datetime.now(timezone.utc).isoformat()}
            await _set_state(db, prefs.id, out)
            return out
        await _set_state(db, prefs.id, {**st, "wants": wants, "state": "reading", "left": len(todo), "at": datetime.now(timezone.utc).isoformat()})
        try:
            reservation = await reserve_global_and_emergency(
                db, "chat_global", max(est, 0.01), config["chat_global_daily_cost_ceiling_usd"], config["chat_global_monthly_cost_ceiling_usd"], config)
        except AIRateLimitExceeded:
            out = {**st, "wants": wants, "state": "error", "left": len(todo), "at": datetime.now(timezone.utc).isoformat()}
            await _set_state(db, prefs.id, out)
            return out
        spent, tin, tout = 0.0, 0, 0
        ai_starred: Dict[str, str] = {}
        try:
            got = await asyncio.gather(*(reader.read(config, w, homes) for w in wants))
            for g in got:
                spent += g["usd"]; tin += g["in"]; tout += g["out"]
            failed = set().union(*(set(g["failed"]) for g in got))
            now = datetime.now(timezone.utc)
            for p, old in todo:
                if p.id in failed:
                    continue   # read again next time
                vs = [g["verdicts"].get(p.id) for g in got]
                v = _all_yes(vs)
                found = [{"w": w, "q": (x or {}).get("quote") or "", "why": (x or {}).get("why") or ""} for w, x in zip(wants, vs) if x]
                was_starred = isinstance(old, dict) and bool(old.get("starred"))
                m = {"k": k, "v": v, "found": found if v == "yes" else [], "starred": was_starred, "at": now.isoformat()}
                if v == "yes" and not was_starred:
                    row = (await db.execute(select(PropertyInteraction).where(and_(
                        PropertyInteraction.collection_id == collection_id, PropertyInteraction.property_id == p.id)))).scalar_one_or_none()
                    if row is None:
                        row = PropertyInteraction(collection_id=collection_id, property_id=p.id, liked=False, disliked=False, created_at=now, updated_at=now)
                        db.add(row)
                    if row.agent_recommended_at is None:
                        row.agent_recommended_at = now
                        row.disliked = False
                        ai_starred[p.id] = now.isoformat()   # the AI's star, not the agent's (ai_only_star_ids)
                    m["starred"] = True
                await db.execute(text("update collection_properties set ai_match = cast(:m as jsonb) where collection_id = :c and property_id = :p"),
                                 {"m": json.dumps(m), "c": collection_id, "p": p.id})
            await db.commit()
            # What this read took of the agent's chat usage - shown on the Showcase like the chat shows it.
            used = reader.percent(spent, limit) if limit else None
            out = {**st, "wants": wants, "state": "done", "left": len(failed), "read": len(todo) - len(failed),
                   "matched": await _count_matched(db, collection_id, k), "at": datetime.now(timezone.utc).isoformat(),
                   "used_percent": used, "used_total_percent": round(float(st.get("used_total_percent") or 0) + (used or 0), 1) if used is not None else st.get("used_total_percent")}
            # The AI's stars, read fresh (the agent may have tapped a star while this read ran - forget_ai_star).
            now_list = (await db.execute(text("select ai_wants->'ai_starred' from collection_preferences where id = :i"), {"i": prefs.id})).scalar()
            out["ai_starred"] = {**(now_list if isinstance(now_list, dict) else {}), **ai_starred}
            await _set_state(db, prefs.id, out)
        except Exception:  # noqa: BLE001 - a failed read leaves the homes unread; the next save or listing tries again
            logger.warning("showcase smart filter failed for %s", collection_id, exc_info=True)
            await db.rollback()
            out = {**st, "wants": wants, "state": "error", "left": len(todo), "at": datetime.now(timezone.utc).isoformat()}
            await _set_state(db, prefs.id, out)
        finally:
            await reconcile_budget(db, reservation, spent)
        if spent:
            await record_usage_event(db, user.id, None, config["model"], tin, tout, spent, 0, False, None, tool_names=["showcase_smart_filter"])
        return out


FOR_SALE = ("ACTIVE-BRIGHT", "COMING SOON-BRIGHT", "ACTIVE", "COMING SOON")
_avg_chars: Dict[str, Any] = {}


async def _avg_desc_chars(db) -> float:
    """The average description length (capped like the reader caps it) of homes for sale - an hour's cache."""
    now = datetime.now(timezone.utc).timestamp()
    if _avg_chars and now - _avg_chars["at"] < 3600:
        return _avg_chars["v"]
    v = (await db.execute(text(
        "select coalesce(avg(least(length(coalesce(description, '')), 1800)), 900) from properties where home_status = any(:s)"),
        {"s": list(FOR_SALE)})).scalar()
    _avg_chars.update(v=float(v or 900), at=now)
    return _avg_chars["v"]


async def estimate(db, user, homes: int, n_wants: int) -> Dict[str, Any]:
    """What reading `homes` homes for `n_wants` must-haves takes - the chat's own Home search math (owner,
    11/09): a percent of the agent's chat usage, what's left, and whether it can run at all (300 homes at
    most, and only when the usage has room). No AI - a plain estimate."""
    from app.services.ai import ai_home_reader as reader
    from app.services.ai.ai_config_service import ai_config_service
    config = await ai_config_service.get_config(db)
    n = max(0, int(homes or 0))
    w = max(1, min(MAX_WANTS, int(n_wants or 1)))
    # Each home's facts + MLS features ride along with its description (~1,300 characters) - as the chat counts it.
    chars = int(n * (await _avg_desc_chars(db) + 1300))
    est = reader.estimate_usd(config, chars, min(n, reader.READ_CAP)) * w if n else 0.0
    left, limit = await reader.room(db, user, config)
    enough = left is None or est <= left
    return {"count": n, "cap": reader.READ_CAP, "usage_percent": reader.percent(est, limit) if limit else 0.0,
            "left_percent": None if left is None else reader.percent(left, limit), "enough": enough,
            "ready": 0 < n <= reader.READ_CAP and enough}


async def showcase_home_count(db, collection_id: str) -> int:
    """Homes for sale in the Showcase the must-haves would read (the agent's removed ones left out)."""
    return int((await db.execute(text(
        "select count(*) from collection_properties cp join properties p on p.id = cp.property_id "
        "where cp.collection_id = :c and cp.sold_filed_at is null and p.home_status = any(:s) "
        "and not exists (select 1 from property_interactions i where i.collection_id = cp.collection_id "
        "and i.property_id = cp.property_id and i.agent_removed_at is not null)"),
        {"c": collection_id, "s": list(FOR_SALE)})).scalar() or 0)


async def ai_only_star_ids(db, collection_id: str, ids) -> set:
    """Of `ids`, the homes whose only reason to stay is a star the must-haves gave (ai_wants.ai_starred - the
    agent touching a home's star takes it off that list, see forget_ai_star) and that the client hasn't
    liked, commented on or toured."""
    ids = [i for i in ids if i]
    if not ids:
        return set()
    rows = (await db.execute(text(
        "select i.property_id from property_interactions i join collection_preferences cpf on cpf.collection_id = i.collection_id "
        "where i.collection_id = :c and i.property_id = any(:ids) and i.agent_recommended_at is not null "
        "and (cpf.ai_wants->'ai_starred') ? i.property_id "
        "and coalesce(i.liked, false) = false "
        "and not exists (select 1 from property_comments c where c.collection_id = i.collection_id and c.property_id = i.property_id) "
        "and not exists (select 1 from property_tours t where t.collection_id = i.collection_id and t.property_id = i.property_id)"),
        {"c": collection_id, "ids": ids})).all()
    return {r[0] for r in rows}


async def forget_ai_star(db, collection_id: str, property_id: str) -> None:
    """The agent starred or unstarred this home by hand - from now on its star is the agent's own."""
    await db.execute(text("update collection_preferences set ai_wants = ai_wants #- array['ai_starred', cast(:p as text)] "
                          "where collection_id = :c and ai_wants ? 'ai_starred'"), {"c": collection_id, "p": property_id})


async def _count_matched(db, collection_id: str, k: str) -> int:
    return int((await db.execute(text(
        "select count(*) from collection_properties where collection_id = :c and ai_match->>'k' = :k and ai_match->>'v' = 'yes'"),
        {"c": collection_id, "k": k})).scalar() or 0)


async def _loop(collection_id: str, delay: float = 0) -> None:
    try:
        if delay:
            await asyncio.sleep(delay)   # the sync's own commit lands first
        while True:
            _again.discard(collection_id)
            await run_for(collection_id)
            if collection_id not in _again:
                break
    except Exception:  # noqa: BLE001
        logger.warning("showcase smart filter loop failed for %s", collection_id, exc_info=True)
        # never left saying "Reading..." for good
        try:
            from app.database import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                await db.execute(text("update collection_preferences set ai_wants = jsonb_set(ai_wants, '{state}', '\"error\"') "
                                      "where collection_id = :c and ai_wants->>'state' = 'reading'"), {"c": collection_id})
                await db.commit()
        except Exception:  # noqa: BLE001
            logger.warning("smart filter state reset failed", exc_info=True)
    finally:
        _running.discard(collection_id)


def schedule(collection_id: str, delay: float = 0) -> None:
    """Read in the background (after the request that saved the search has committed)."""
    if collection_id in _running:
        _again.add(collection_id)
        return
    _running.add(collection_id)
    try:
        asyncio.get_running_loop().create_task(_loop(collection_id, delay))
    except RuntimeError:
        _running.discard(collection_id)


async def schedule_if_on(db, collection_ids, delay: float = 0) -> None:
    """After new listings land: only the Showcases that have must-haves."""
    ids = list({c for c in collection_ids if c})
    if not ids:
        return
    from app.models.database import CollectionPreferences
    rows = (await db.execute(select(CollectionPreferences.collection_id, CollectionPreferences.ai_wants)
                             .where(CollectionPreferences.collection_id.in_(ids)))).all()
    for cid, v in rows:
        if clean_wants(v):
            schedule(cid, delay)


_MARKED = re.compile(r"⟦(.+?)⟧")


def match_line(m: Any) -> Optional[Dict[str, Any]]:
    """What a home card shows: only a 'yes' (changing the must-haves clears every home's read - save_wants).
    marks = the listing's own words the AI pointed at, one per must-have (the ⟦key words⟧ of its quote) - the
    home's details window highlights them in the description (owner, 11/09). A must-have taken off clears
    the read, so its highlight goes with it."""
    if not isinstance(m, dict) or m.get("v") != "yes":
        return None
    found = m.get("found") or []
    marks = []
    for f in found:
        hit = _MARKED.search(f.get("q") or "")
        if f.get("w") and hit and hit.group(1).strip():
            marks.append({"want": f["w"], "words": hit.group(1).strip()})
    return {"wants": [f.get("w") for f in found if f.get("w")],
            "quotes": [f.get("q") for f in found if f.get("q")][:3],
            "marks": marks[:MAX_WANTS]}


async def save_wants(db, collection_id: str, raw: Any) -> Dict[str, Any]:
    """The agent saved its must-haves: new ones clear every home's read (the stars stay - the agent's to
    take off) and reading starts in the background. Empty turns it off. Caller commits nothing."""
    from app.models.database import CollectionPreferences
    prefs = (await db.execute(select(CollectionPreferences).where(CollectionPreferences.collection_id == collection_id))).scalar_one_or_none()
    if prefs is None:
        return {}
    wants = clean_wants(raw)
    old = state_of(prefs)
    changed = wants_key(wants) != wants_key(clean_wants(old.get("wants")))
    st = {**old, "wants": wants, "state": ("reading" if changed or old.get("state") != "done" else "done") if wants else "off",
          "at": datetime.now(timezone.utc).isoformat()}
    if changed:
        st.pop("matched", None); st.pop("read", None)
        await db.execute(text("update collection_properties set ai_match = null where collection_id = :c"), {"c": collection_id})
    await db.execute(text("update collection_preferences set ai_wants = cast(:v as jsonb) where id = :i"), {"v": json.dumps(st), "i": prefs.id})
    await db.commit()
    if wants:
        schedule(collection_id)
    return st
