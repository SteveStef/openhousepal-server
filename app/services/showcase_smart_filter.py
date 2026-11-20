"""Showcase smart filter (owner, 11/09): "9 ft+ ceilings", "pool", "first-floor primary" - up to 3 things the
agent wants on top of a Showcase's regular search. The regular search still decides which homes are in the
Showcase; the AI listing reader (services/ai/ai_home_reader.py - the chat's own, with its one-level-home and
"at least" rules) reads each home once for each thing, and a home that has ALL of them is starred, as if the
agent had recommended it, with a line under its address: "Appears to have: first-floor primary bedroom".
The other homes stay, without the star or the line.

- Runs after the agent saves the search or the must-haves, and when a new listing lands in the Showcase.
  Each home is read once per set of must-haves (collection_properties.ai_match.k) - a new listing costs
  one home's read, not the whole Showcase's.
- Costs the agent's AI usage, like the chat's own listing reads: checked against what's left before
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


_TIDY_SYSTEM = (
    "A real-estate agent typed things a buyer must have in a home (they may be misspelled or rough). For each one, in order, "
    "return: label = the same thing, spelled right, short, first letter capital (e.g. 'col de sack' -> 'Cul-de-sac', "
    "'no hoa' -> 'No HOA', 'near train' -> 'Near a train station'); many = words finishing '2 of them ...' (e.g. 'are on a "
    "cul-de-sac', 'have a pool', 'have no HOA', 'are near a train station', 'have 9 ft+ ceilings', 'are on lots under 1 acre'); "
    "one = the same for one home ('is on a cul-de-sac', 'has a pool', 'has no HOA', 'is on a lot under 1 acre'); it = words "
    "finishing 'This home appears to ...' ('be on a cul-de-sac', 'have a pool', 'have no HOA', 'be on a lot under 1 acre'). "
    "Each must read as natural, correct English in its sentence. Keep the meaning exactly - never add, soften or exaggerate. "
    "Lowercase start for many/one/it, no ending period, no lead-in words. Answer with JSON only: "
    "{\"items\":[{\"label\":\"...\",\"many\":\"...\",\"one\":\"...\",\"it\":\"...\"}]}"
)


def join_clauses(clauses: List[str]) -> str:
    """'have no HOA', 'have a pool' -> 'have no HOA and a pool'; a verb said again right after itself is dropped."""
    out = []
    for i, c in enumerate(clauses):
        prev = clauses[i - 1].split(" ", 1)[0] if i else None
        first, _, rest = c.partition(" ")
        out.append(rest if prev and first == prev and rest else c)
    return out[0] if len(out) == 1 else ", ".join(out[:-1]) + " and " + out[-1]


async def tidy_wants(db, user, wants: List[str], known: Optional[Dict[str, Any]] = None) -> tuple:
    """(wants with the spelling fixed, {label: {many, one, it}}) - the words a follow-up text and a home card put
    them in (owner, 11/09: "col de sack" must never reach a client like that). One small AI call for the ones it
    hasn't seen; known = phrases already worked out. Any failure keeps the agent's words and no phrases - the
    sentences then fall back to "match what you asked for: ..." (tasks_service._must_parts)."""
    known = {k.lower(): (k, v) for k, v in (known or {}).items() if isinstance(v, dict)}
    out_wants, phrases, todo = [], {}, []
    for w in wants:
        hit = known.get(w.lower())
        if hit:
            out_wants.append(hit[0]); phrases[hit[0]] = hit[1]
        else:
            out_wants.append(w); todo.append(w)
    if not todo:
        return out_wants, phrases
    try:
        from app.services.ai import ai_home_reader as reader
        from app.services.ai.ai_config_service import ai_config_service
        config = await ai_config_service.get_config(db)
        resp = await asyncio.wait_for(reader._get_client().messages.create(
            model=config["model"], max_tokens=600, system=_TIDY_SYSTEM,
            messages=[{"role": "user", "content": "\n".join(f"{i + 1}. {w}" for i, w in enumerate(todo))}],
            **ai_config_service.model_options(config["model"], "low")), timeout=12)
        text_out = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        m = re.search(r"\{.*\}", text_out, re.S)
        items = (json.loads(m.group(0)).get("items") if m else None) or []
        try:
            from app.services.ai.ai_rate_limit_service import record_usage_event
            usd = ai_config_service.estimate_cost_usd(config, config["model"], resp.usage.input_tokens, resp.usage.output_tokens)
            await record_usage_event(db, user.id, None, config["model"], resp.usage.input_tokens, resp.usage.output_tokens, usd, 0, False, None,
                                     tool_names=["showcase_smart_filter_tidy"])
        except Exception:  # noqa: BLE001 - the words still count; the usage note is best-effort
            logger.warning("smart filter tidy usage not recorded", exc_info=True)
    except Exception:  # noqa: BLE001
        logger.warning("smart filter tidy failed - keeping the agent's words", exc_info=True)
        return out_wants, phrases

    def ok_phrase(s) -> Optional[str]:
        s = " ".join(str(s or "").split()).rstrip(".")
        # it sometimes repeats the lead-in ("This home appears to have a pool") or starts with a capital
        s = re.sub(r"^(this home appears to|this appears to|it appears to|2 of them|one of them|the home|they|it)\s+", "", s, flags=re.I)
        first = s.split(" ", 1)[0] if s else ""
        if first and not first.isupper():
            s = s[:1].lower() + s[1:]
        return s if 2 <= len(s) <= 80 and s[0].islower() and not re.search(r"[{}<>\[\]]", s) else None

    for w, it in zip(todo, items if isinstance(items, list) else []):
        if not isinstance(it, dict):
            continue
        label = " ".join(str(it.get("label") or "").split()).strip(" .")[:MAX_WANT_CHARS]
        p = {k: ok_phrase(it.get(k)) for k in ("many", "one", "it")}
        if not label or re.search(r"[{}<>\[\]]", label):
            label = w
        label = label[:1].upper() + label[1:]
        i = out_wants.index(w)
        if any(x.lower() == label.lower() for j, x in enumerate(out_wants) if j != i):
            continue   # two wants turned into the same thing - keep the agent's words
        out_wants[i] = label
        if all(p.values()):
            phrases[label] = p
    return out_wants, phrases


async def fill_phrases(db, collection_id: str, user, st: Dict[str, Any]) -> Dict[str, Any]:
    """Catch-up for must-haves saved before tidy_wants existed (owner, 11/09 - Nicole's): give them their sentence
    words once, keyed by the words as saved. The must-haves themselves are NOT respelled here - that would
    change the read key and make every home be read again. Writes only the phrases (never clobbers the rest)."""
    wants = clean_wants(st.get("wants"))
    have = st.get("phrases") if isinstance(st.get("phrases"), dict) else {}
    if not wants or all(w in have for w in wants) or user is None:
        return have
    new_wants, ph = await tidy_wants(db, user, wants, have)
    mapped = {**have}
    for orig, new in zip(wants, new_wants):
        if new in ph and orig not in mapped:
            mapped[orig] = ph[new]
    mapped = {w: p for w, p in mapped.items() if w in wants}
    if mapped != have:
        await db.execute(text("update collection_preferences set ai_wants = jsonb_set(ai_wants, '{phrases}', cast(:p as jsonb)) "
                              "where collection_id = :c and ai_wants is not null"), {"p": json.dumps(mapped), "c": collection_id})
        await db.commit()
    return mapped


async def catch_up_phrases() -> int:
    """Once after a deploy: every Showcase with must-haves but no sentence words gets them (a fraction of a cent each)."""
    from app.database import AsyncSessionLocal
    from app.models.database import Collection, User
    done = 0
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(text(
                "select cp.collection_id, cp.ai_wants, c.owner_id from collection_preferences cp join collections c on c.id = cp.collection_id "
                "where jsonb_typeof(cp.ai_wants->'wants') = 'array' and jsonb_array_length(cp.ai_wants->'wants') > 0 "
                "and coalesce(cp.ai_wants->>'state', '') not in ('off', 'too_many')"))).all()
            for cid, st, owner_id in rows:
                st = st if isinstance(st, dict) else {}
                have = st.get("phrases") if isinstance(st.get("phrases"), dict) else {}
                if all(w in have for w in clean_wants(st.get("wants"))):
                    continue
                user = await db.get(User, owner_id)
                if user is not None:
                    await fill_phrases(db, cid, user, st)
                    done += 1
    except Exception:  # noqa: BLE001 - the plain wording still reads right; next deploy tries again
        logger.warning("smart filter phrase catch-up failed", exc_info=True)
    if done:
        logger.info("smart filter: sentence words added for %s Showcase(s)", done)
    return done


_PLACE_RX = re.compile(r"^(?:(?:is |be |located |must be |homes? )?in(?: the)? )?(.+?)(?:\s+(township|twp|borough|boro|town|city))?$", re.I)


async def place_wants(db, wants: List[str]) -> Dict[str, Dict[str, Any]]:
    """Words that name a place ("In Uwchlan Township", "Chester Springs"), found in the listings' own townships /
    towns. Since 11/20 a place is never a must-have (it never stars a home) - split_filter_wants moves it into the
    search's Townships / Towns filter."""
    from app.models.database import Property
    out: Dict[str, Dict[str, Any]] = {}
    for w in wants:
        m = _PLACE_RX.match(" ".join(w.split()))
        if not m:
            continue
        name, kind = m.group(1).strip(" ,.").upper(), (m.group(2) or "").lower()
        if len(name) < 3 or len(name.split()) > 4:
            continue
        nice = " ".join(x[:1] + x[1:].lower() for x in name.split())
        if kind in ("", "township", "twp", "borough", "boro"):
            hit = (await db.execute(select(Property.township).where(
                Property.township.in_([name, f"{name} TWP", f"{name} TOWNSHIP", f"{name} BORO", f"{name} BOROUGH"])).limit(1))).scalar()
            if hit:
                word = {"twp": "Township", "boro": "Borough"}.get(kind, kind.title())
                out[w] = {"field": "township", "values": {name, f"{name} TWP", f"{name} TOWNSHIP", f"{name} BORO", f"{name} BOROUGH"},
                          "say": f"In {nice}" + (f" {word}" if word else (" Township" if hit.upper().endswith(("TWP", "TOWNSHIP")) else "")),
                          "name": nice}
                continue
        if kind in ("", "town", "city"):
            hit = (await db.execute(select(Property.city, Property.state).where(Property.city == name).limit(1))).first()
            if hit:
                out[w] = {"field": "city", "values": {name}, "say": f"In {nice}", "name": f"{nice}, {hit.state}" if hit.state else nice}
    return out


_SCHOOL_WANT = re.compile(r"\b(high\s*schools?|hs|schools?|school\s+district|district)\b", re.I)
_NEAR_WORDS = re.compile(r"\b(walk|walking|walkable|near|nearby|close|bus|minutes?|mins?|drive|driving|across|blocks?|mile|miles)\b", re.I)


async def split_filter_wants(db, wants: List[str], districts: Optional[List[str]] = None) -> tuple:
    """Owner 11/20: "a township actually shouldn't be a must-have - it's a basic sorting feature now". Must-haves are
    features the search can't see; a township / town / school is a filter. Returns (feature wants, moves):
    a move = {"want", "to": "townships"|"cities"|"high_schools"|"school_note", "value", "say"}. A school want becomes
    the High school filter only when the Showcase has a district and exactly one of its schools fits; otherwise it
    is a note ("pick the high school under School districts") - never a guessed filter, never an AI read (a listing
    that leaves the school out could never be starred, and "DOWNINGTOWN" was once read as East)."""
    places = await place_wants(db, wants)
    keep, moves = [], []
    opts = None
    for w in wants:
        p = places.get(w)
        if p and not _SCHOOL_WANT.search(w):
            moves.append({"want": w, "to": "townships" if p["field"] == "township" else "cities", "value": p["name"],
                          "say": p["say"].replace("In ", "", 1)})
            continue
        if _SCHOOL_WANT.search(w) and not _NEAR_WORDS.search(w):
            hit = None
            if districts:
                from app.services import home_school_service as hs
                if opts is None:
                    opts = await hs.schools_in(db, districts)
                hit = hs.match_school_name(opts, w)
            if hit:
                moves.append({"want": w, "to": "high_schools", "value": hit["key"], "say": hit["name"]})
            else:
                moves.append({"want": w, "to": "school_note", "value": None, "say": w})
            continue
        keep.append(w)
    return keep, moves


def moves_line(moves: List[Dict[str, Any]]) -> str:
    """One plain sentence for the agent: where each township / town / school went."""
    parts = []
    for m in moves:
        if m["to"] == "townships":
            parts.append(f"{m['say']} is now a Townships filter")
        elif m["to"] == "cities":
            parts.append(f"{m['say']} is now a Towns filter")
        elif m["to"] == "high_schools":
            parts.append(f"{m['say']} is now the High school filter")
        else:
            parts.append(f"“{m['want']}” isn't a must-have - pick the school district and high school in the search instead")
    return "; ".join(parts)


async def apply_filter_moves(db, prefs, moves: List[Dict[str, Any]]) -> bool:
    """Puts the moved townships / towns / high school into the Showcase's search. True if the search changed."""
    changed = False
    for field in ("townships", "cities", "high_schools"):
        add = [m["value"] for m in moves if m["to"] == field and m.get("value")]
        if not add:
            continue
        cur = list(getattr(prefs, field, None) or [])
        low = {str(x).split(",")[0].strip().lower() for x in cur}
        new = cur + [v for v in add if str(v).split(",")[0].strip().lower() not in low]
        if new != cur:
            setattr(prefs, field, new)
            changed = True
    if changed:
        await db.flush()
    return changed


def place_verdict(p, place: Dict[str, Any]) -> Dict[str, Any]:
    v = (getattr(p, place["field"], None) or "").upper().strip()
    return {"v": "yes", "quote": "", "why": place["say"], "place": place["say"]} if v in place["values"] else {"v": "no", "quote": "", "why": ""}


async def wants_for(db, collection_ids) -> Dict[str, List[str]]:
    """{Showcase id: its must-haves} - one read, for lists that show them (visitor rows, the lead page)."""
    ids = [i for i in (collection_ids or []) if i]
    if not ids:
        return {}
    rows = (await db.execute(text("select collection_id, ai_wants from collection_preferences where collection_id = any(:ids)"),
                             {"ids": ids})).all()
    out: Dict[str, List[str]] = {}
    for cid, st in rows:
        st = st if isinstance(st, dict) else {}
        w = clean_wants(st.get("wants")) if st.get("state") != "off" else []
        if w:
            out[cid] = w
    return out


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
        feature, old_filters = await split_filter_wants(db, wants)
        if old_filters:
            # saved before 11/20 (Lisa's "Downingtown East schools"): a filter, never a star
            await take_off_ai_stars(db, collection_id, st)
            await db.execute(text("update collection_properties set ai_match = case when ai_match ? 'per' "
                                  "then jsonb_build_object('per', ai_match->'per') else null end where collection_id = :c"), {"c": collection_id})
            st = {**{k2: v2 for k2, v2 in st.items() if k2 not in ("matched", "read")}, "wants": feature,
                  "state": "reading" if feature else "off", "at": datetime.now(timezone.utc).isoformat(),
                  "phrases": {w2: p2 for w2, p2 in (st.get("phrases") or {}).items() if w2 in feature}}
            await _set_state(db, prefs.id, st)
            wants = feature
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
        # older must-haves: sentence words, once (after the 300 check - a paused Showcase uses no AI)
        try:
            st["phrases"] = await fill_phrases(db, collection_id, user, st)
        except Exception:  # noqa: BLE001
            logger.warning("smart filter phrases catch-up failed for %s", collection_id, exc_info=True)
        if not todo:
            done = {**st, "wants": wants, "state": "done", "left": 0, "at": datetime.now(timezone.utc).isoformat(),
                    "matched": await _count_matched(db, collection_id, k)}
            await _set_state(db, prefs.id, done)
            return done

        config = await ai_config_service.get_config(db)
        homes = [p for p, _ in todo]
        # Places never star a home since 11/20 (they're search filters - dropped above); every must-have is a feature.
        places: Dict[str, Dict[str, Any]] = {}
        read_wants = [w for w in wants if w not in places]
        est = reader.estimate_for(config, homes) * len(read_wants)
        left, limit = await reader.room(db, user, config)
        if read_wants and left is not None and est > left:
            out = {**st, "wants": wants, "state": "no_room", "left": len(todo), "at": datetime.now(timezone.utc).isoformat()}
            await _set_state(db, prefs.id, out)
            return out
        await _set_state(db, prefs.id, {**st, "wants": wants, "state": "reading", "left": len(todo), "at": datetime.now(timezone.utc).isoformat()})
        reservation = None
        if read_wants:
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
            # Cheapest way to the same stars (owner, 11/19): a star needs EVERY must-have, so the free place checks go
            # first, then each AI must-have is read only for the homes that passed the ones before it; and each home's
            # answer per must-have is kept, so changing the set only reads what's new.
            per: Dict[str, Dict[str, dict]] = {}
            for p, old in todo:
                prev = old.get("per") if isinstance(old, dict) and isinstance(old.get("per"), dict) else {}
                per[p.id] = {w.lower(): prev[w.lower()] for w in read_wants if w.lower() in prev}
                for w in wants:
                    if w in places:
                        per[p.id][w.lower()] = place_verdict(p, places[w])
            dead = lambda pid: any((x or {}).get("v") != "yes" for x in per[pid].values())  # noqa: E731
            failed: set = set()
            for w in read_wants:
                need = [p for p in homes if p.id not in failed and not dead(p.id) and w.lower() not in per[p.id]]
                if not need:
                    continue
                g = await reader.read(config, w, need)
                spent += g["usd"]; tin += g["in"]; tout += g["out"]
                failed |= set(g["failed"])
                for p in need:
                    if p.id in set(g["read"]):
                        x = g["verdicts"].get(p.id)
                        per[p.id][w.lower()] = {"v": x["v"], "quote": x.get("quote") or "", "why": x.get("why") or ""} if x else {"v": "no"}
            now = datetime.now(timezone.utc)
            for p, old in todo:
                if p.id in failed:
                    continue   # read again next time
                vs = [per[p.id].get(w.lower()) for w in wants]
                vs = [None if (x or {}).get("v") == "no" else x for x in vs]
                v = _all_yes(vs)
                found = [{"w": w, "q": (x or {}).get("quote") or "", "why": (x or {}).get("why") or "", **({"place": x["place"]} if x.get("place") else {})}
                         for w, x in zip(wants, vs) if x]
                was_starred = isinstance(old, dict) and bool(old.get("starred"))
                m = {"k": k, "v": v, "found": found if v == "yes" else [], "starred": was_starred, "at": now.isoformat(),
                     "per": {wk: x for wk, x in per[p.id].items() if not (x or {}).get("place")}}
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
            # What this read took of the agent's AI usage - shown on the Showcase like the chat shows it.
            used = reader.percent(spent, limit) if limit else None
            out = {**st, "wants": wants, "state": "done", "left": len(failed), "read": len(todo) - len(failed),
                   "matched": await _count_matched(db, collection_id, k), "at": datetime.now(timezone.utc).isoformat(),
                   "used_percent": used, "used_total_percent": round(float(st.get("used_total_percent") or 0) + (used or 0), 1) if used is not None else st.get("used_total_percent")}
            # The AI's stars, read fresh (the agent may have tapped a star while this read ran - forget_ai_star).
            now_list = (await db.execute(text("select ai_wants->'ai_starred' from collection_preferences where id = :i"), {"i": prefs.id})).scalar()
            out["ai_starred"] = {**(now_list if isinstance(now_list, dict) else {}), **ai_starred}
            await _set_state(db, prefs.id, out)
            # their follow-up text names the must-haves - rebuild it now, not a minute from now (tasks_service)
            try:
                from app.services.tasks_service import forget_pitch
                forget_pitch(coll.share_token)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001 - a failed read leaves the homes unread; the next save or listing tries again
            logger.warning("showcase smart filter failed for %s", collection_id, exc_info=True)
            await db.rollback()
            out = {**st, "wants": wants, "state": "error", "left": len(todo), "at": datetime.now(timezone.utc).isoformat()}
            await _set_state(db, prefs.id, out)
        finally:
            if reservation is not None:
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
    11/09): a percent of the agent's AI usage, what's left, and whether it can run at all (300 homes at
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


async def take_off_ai_stars(db, collection_id: str, st: Dict[str, Any]) -> int:
    """The must-haves changed: the stars the old ones gave come off (owner, 11/20: "starred means it's likely perfect,
    not just a match"). Only the AI's own stars (ai_wants.ai_starred) - a star the agent gave or touched by hand
    stays. The new must-haves star what fits them. Caller commits."""
    ids = [i for i in (st.get("ai_starred") or {}) if i]
    if ids:
        await db.execute(text("update property_interactions set agent_recommended_at = null where collection_id = :c "
                              "and property_id = any(:ids) and agent_recommended_at is not null"), {"c": collection_id, "ids": ids})
    st["ai_starred"] = {}
    return len(ids)


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


_LEASE, _AGAIN, _LEASE_TTL = "smart_filter", "smart_filter_again", 120


async def _passes(collection_id: str) -> bool:
    """Read while holding the Showcase's shared lease - one copy of the API at a time (11/14: two copies
    reading the same Showcase at once paid for the same AI reading twice). False = another copy is on it."""
    from app.services import cluster
    async with cluster.lease(_LEASE, collection_id, _LEASE_TTL) as token:
        if token is None:
            return False
        while True:
            _again.discard(collection_id)
            await cluster.release(_AGAIN, collection_id)
            await run_for(collection_id)
            if collection_id not in _again and not await cluster.alive(_AGAIN, collection_id):
                return True


async def _loop(collection_id: str, delay: float = 0) -> None:
    from app.services import cluster
    try:
        if delay:
            await asyncio.sleep(delay)   # the sync's own commit lands first
        if not await _passes(collection_id):
            # Another copy is reading it: ask it for one more pass (it checks before it stops), then try
            # once more ourselves in case it stopped just before seeing the note.
            await cluster.acquire(_AGAIN, collection_id, 900)
            if not await _passes(collection_id):
                return
        if await cluster.alive(_AGAIN, collection_id):   # asked again just as we were stopping
            await _passes(collection_id)
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
            "marks": marks[:MAX_WANTS],
            # places the listing itself says ("In Uwchlan Township") - shown as plain facts, not "appears to"
            "facts": [f["place"] for f in found if f.get("place")],
            "place_wants": [f.get("w") for f in found if f.get("place")]}


async def save_wants(db, collection_id: str, raw: Any, user=None) -> Dict[str, Any]:
    """The agent saved its must-haves: new ones clear every home's read (the stars stay - the agent's to
    take off) and reading starts in the background. Empty turns it off. Caller commits nothing.
    With the agent (user), new wording is tidied first (tidy_wants): spelling fixed, sentence words kept."""
    from app.models.database import CollectionPreferences
    prefs = (await db.execute(select(CollectionPreferences).where(CollectionPreferences.collection_id == collection_id))).scalar_one_or_none()
    if prefs is None:
        return {}
    wants = clean_wants(raw)
    old = state_of(prefs)
    phrases = {}
    # townships / towns / schools are search filters, not must-haves (owner, 11/20) - checked before and after tidying
    wants, moves = await split_filter_wants(db, wants, list(prefs.school_districts or []))
    if wants and user is not None:
        wants, phrases = await tidy_wants(db, user, wants, old.get("phrases"))
        wants, more = await split_filter_wants(db, clean_wants(wants), list(prefs.school_districts or []))
        moves += more
    search_changed = await apply_filter_moves(db, prefs, moves) if moves else False
    changed = wants_key(wants) != wants_key(clean_wants(old.get("wants")))
    st = {**old, "wants": wants, "state": ("reading" if changed or old.get("state") != "done" else "done") if wants else "off",
          "at": datetime.now(timezone.utc).isoformat()}
    if user is not None or not wants:
        # only the words for the current must-haves (a follow-up / card can never use stale ones)
        st["phrases"] = {w: p for w, p in {**(old.get("phrases") or {}), **phrases}.items() if w in wants}
    if changed:
        st.pop("matched", None); st.pop("read", None)
        await take_off_ai_stars(db, collection_id, st)
        # each home's answer per must-have stays (run_for reuses it - only new must-haves are read); the line goes
        await db.execute(text("update collection_properties set ai_match = case when ai_match ? 'per' "
                              "then jsonb_build_object('per', ai_match->'per') else null end where collection_id = :c"), {"c": collection_id})
    st.pop("moved", None)
    if moves:
        st["moved"] = moves
    await db.execute(text("update collection_preferences set ai_wants = cast(:v as jsonb) where id = :i"), {"v": json.dumps(st), "i": prefs.id})
    await db.commit()
    if search_changed:
        # the moved township / town / school changes which homes are in it
        from app.services.collections_service import CollectionsService
        try:
            await CollectionsService.repopulate_collection_from_preferences(db, collection_id, commit=True)
        except Exception:
            logger.warning("refresh after a must-have became a filter failed", exc_info=True)
            await db.rollback()
    if wants:
        schedule(collection_id)
    return st
