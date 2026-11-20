"""Seller CMA, two paths (owner 11/19).

With inside photos of the home, the agent shouldn't have to answer the Compare step by hand: the AI reads the
photos first and fills the answers in - each comp nicer / about the same / not as nice, and the kitchen and the
baths - and the agent checks them (or changes one) before the report is made. Without inside photos the agent
answers, as before. Rooms the photos don't show, and comps the AI couldn't judge, are left for the agent.

The read is the report's own comp/photo pass (cma_ai_service.generate_ai_analysis(photo_read_only=True)), run
once, before the agent answers, with nothing the agent said in front of it. It is kept with the report and the
report uses it instead of reading the photos again (cma_ai_service._seller_read_get). The value is always made
from the ANSWERS, never from the photos directly - the photos only suggest the answers.

Tested on 5 sold homes on the full sales data (11/19): the AI's own comp marks matched the agent's on 23 of 25
comps, and used as the answers they priced the homes about as well (4.2% average miss vs 4.5%).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_STATE_KEY = "cma_seller_prefill:{}"
# A running read renews its state every minute (beat_at); one not renewed for this long was cut off (a restart, a
# push): the agent is offered to try again instead of waiting on it.
BEAT_SECONDS = 60
STALE_READ_MINUTES = 3
_MARK_OF = {"below": -1, "similar": 0, "above": 1}
_ROOM_STATES = ("original", "updated", "redone")
_BACKGROUND: set = set()


def mark_from_quality(q: Optional[dict]) -> Optional[str]:
    """The AI's rating of a comp (finish / interior: 'above' = the comp is nicer) as the agent's answer for it
    (this home is 'better' than / the 'same' as / 'worse' than the comp). None = the AI couldn't tell."""
    vals = [v for v in ((q or {}).get(f) for f in ("finish_quality", "interior")) if v in _MARK_OF]
    if not vals:
        return None
    score = sum(_MARK_OF[v] for v in vals)
    return "worse" if score > 0 else "better" if score < 0 else "same"


def comp_key(c: dict) -> str:
    return str((c or {}).get("listing_key") or (c or {}).get("id") or "")


def included_comp_keys(comps) -> list:
    return sorted(comp_key(c) for c in (comps or []) if isinstance(c, dict) and c.get("is_included", True))


def suggestions(result: dict, rooms_sent: Optional[list], comps: list) -> dict:
    """What the read suggests for each question. `rooms_sent` = the rooms the AI was shown of the home (photo
    sort; None = not sorted). A room the photos don't show is never filled in - the agent is asked."""
    by_key = {str(c.get("listing_key") or ""): c for c in (result or {}).get("comps") or [] if isinstance(c, dict)}
    marks, unread = {}, []
    for c in comps or []:
        if not isinstance(c, dict) or not c.get("is_included", True):
            continue
        k = comp_key(c)
        m = mark_from_quality((by_key.get(str(c.get("listing_key") or "")) or {}).get("quality"))
        if m:
            marks[k] = m
        else:
            unread.append(k)
    rooms = (result or {}).get("subject_rooms")
    rooms = rooms if isinstance(rooms, dict) else {}
    shown = set(rooms_sent) if isinstance(rooms_sent, (list, tuple, set)) else None
    out: dict = {}
    for key, photo_room in (("kitchen", "kitchen"), ("baths", "bathroom")):
        v = rooms.get(key)
        if v in _ROOM_STATES and (shown is None or photo_room in shown):
            out[key] = v
    return {"comp_marks": marks, "unread": unread, "kitchen": out.get("kitchen"), "baths": out.get("baths"),
            "rooms_missing": [k for k in ("kitchen", "baths") if k not in out]}


def fill_blanks(subject: Optional[dict], result: dict, rooms_sent: Optional[list], comps: list) -> Optional[dict]:
    """One-pass seller report (owner 11/19, "stupid simple"): the agent said the photos show the home as it is today
    (seller_answers.inside_photos == "yes") and pressed Create - no separate read, no stop. The report's own photo
    pass answers every comp and room the agent left blank; the agent's own answers are never touched. The ones the
    AI filled are listed in ai_filled (the report credits them to the photos, not to the agent; the agent can change
    any of them on the finished report - Update the value). Returns the new subject, or None when nothing to do."""
    s = subject or {}
    sa = dict(s.get("seller_answers") or {}) if isinstance(s.get("seller_answers"), dict) else {}
    if sa.get("inside_photos") != "yes":
        return None
    sg = suggestions(result, rooms_sent, comps)
    marks = {str(k): str(v) for k, v in (sa.get("comp_marks") or {}).items()} if isinstance(sa.get("comp_marks"), dict) else {}
    filled = [str(x) for x in (sa.get("ai_filled") or [])]
    for k, m in sg["comp_marks"].items():
        if k not in marks:
            marks[k] = m
            filled.append(k)
    for room in ("kitchen", "baths"):
        if sg.get(room) and sa.get(room) in (None, "", "not_sure"):
            sa[room] = sg[room]
            filled.append(room)
    sa["comp_marks"] = marks
    sa["ai_filled"] = sorted(set(filled))
    sa["ai_unread"] = [k for k in sg["unread"] if k not in marks]
    sa["ai_rooms_missing"] = [r for r in sg["rooms_missing"] if sa.get(r) in (None, "", "not_sure")]
    return {**s, "seller_answers": sa}


def core_facts_missing(subject: Optional[dict], mode: Optional[str]) -> list:
    """A seller home typed in by hand (not on the MLS - Bright has no public records): the six facts the value is
    built on must be filled in before anything is spent (owner 11/19). Same list as the Home step's
    requiredMissingFacts. Empty for a buyer report or an MLS home."""
    s = subject or {}
    if mode == "buyer" or s.get("source") != "manual" or s.get("listing_key"):
        return []
    out = []
    if s.get("latitude") is None or s.get("longitude") is None:
        out.append("map location")
    if not s.get("property_type"):
        out.append("property type")
    if s.get("beds") is None:
        out.append("bedrooms")
    if s.get("baths") is None:
        out.append("bathrooms")
    if not (s.get("above_grade_finished_area") or s.get("sqft")):
        out.append("above-grade square feet")
    if not s.get("year_built"):
        out.append("year built")
    return out


def core_facts_message(missing: list) -> str:
    return (f"Fill in {', '.join(missing)} on the Home step first - this home isn't on the MLS, so the value is built "
            "on what you enter.")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_state(db, report_id) -> Optional[dict]:
    from app.services.cma_ai_service import _reasoning_cache_get
    got = await _reasoning_cache_get(db, _STATE_KEY.format(report_id))
    return got if isinstance(got, dict) else None


async def _put_state(report_id, state: dict) -> None:
    from app.services.cma_ai_service import _reasoning_cache_put
    await _reasoning_cache_put(None, _STATE_KEY.format(report_id), state)


def public_state(report, state: Optional[dict]) -> dict:
    """The read as the Compare step sees it. `stale` = the comps or the photos changed since it was made - its
    answers may not fit any more, and the report would not reuse it."""
    from app.services.cma_ai_service import _seller_photos_sig
    if not state:
        return {"status": "none"}
    out = dict(state)
    if out.get("status") == "reading":
        try:
            began = datetime.fromisoformat(str(out.get("beat_at") or out.get("started_at")))
        except ValueError:
            began = None
        if began is None or (_now() - began).total_seconds() > STALE_READ_MINUTES * 60:
            out.update(status="failed", error="Reading the photos stopped partway. Try again, or answer the questions yourself.")
    out["stale"] = bool(out.get("status") == "ready" and (
        out.get("comp_keys") != included_comp_keys(getattr(report, "comparables", None))
        or out.get("photos") != _seller_photos_sig(getattr(report, "subject", None) or {})))
    return out


async def start(db, user, report, config, save_usage: bool) -> dict:
    """POST: checks, the per-report run hold, then the read in the background. Returns the state at once."""
    from fastapi import HTTPException
    from app.services.ai.ai_rate_limit_service import (AIRateLimitExceeded, CmaGenerationAlreadyRunning,
                                                       hold_cma_generation, release_cma_generation)
    from app.services.cma_ai_service import _has_completed_ai_pricing, _seller_photos_sig
    if report.mode == "buyer":
        raise HTTPException(status_code=400, detail="Reading the photos first is for a seller report.")
    if getattr(report, "ai_analysis", None) and _has_completed_ai_pricing(report):
        raise HTTPException(status_code=400, detail="This report already has its AI analysis.")
    if report.generation_status == "generating":
        raise HTTPException(status_code=409, detail="This report is being made right now.")
    subj = report.subject or {}
    _core = core_facts_missing(subj, report.mode)
    if _core:
        raise HTTPException(status_code=422, detail=core_facts_message(_core))
    if not [p for p in (subj.get("condition_photos") or []) + (subj.get("photos") or []) if p]:
        raise HTTPException(status_code=422, detail="There are no photos of this home to read. Add photos, or answer the questions yourself.")
    if len([c for c in (report.comparables or []) if c.get("is_included", True)]) < 2:
        raise HTTPException(status_code=422, detail="Pick at least 2 comparable sales first.")
    cur = public_state(report, await get_state(db, report.id))
    if cur.get("status") == "reading":
        return cur                       # a second press (or a second tab): the read already going
    try:
        await hold_cma_generation(db, user.id, str(report.id), config, user.plan_tier)
    except CmaGenerationAlreadyRunning as e:
        raise HTTPException(status_code=409, detail=e.user_message)
    except AIRateLimitExceeded as e:
        raise HTTPException(status_code=429, detail=e.user_message)
    state = {"status": "reading", "started_at": _now().isoformat(), "save_usage": bool(save_usage),
             "comp_keys": included_comp_keys(report.comparables), "photos": _seller_photos_sig(subj)}
    try:
        await _put_state(report.id, state)
        address = str(subj.get("street_address") or "your home")
        task = asyncio.create_task(_run(str(report.id), user.id, user.plan_tier, bool(save_usage), config, address, state))
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)
    except BaseException:
        await release_cma_generation(str(report.id))
        raise
    return public_state(report, state)


async def _run(report_id: str, owner_id: str, tier, save_usage: bool, config: dict, address: str, state: dict) -> None:
    from fastapi import HTTPException
    from app.database import AsyncSessionLocal
    from app.services import cma_claude, cma_save_usage
    from app.services.ai.ai_rate_limit_service import AIRateLimitExceeded, release_cma_generation
    from app.api.cma_routes import _generate_maybe_as_of
    opts = dict(max_photos_per_property=None, include_map_images=True, force_regenerate=False, photo_read_only=True)
    err = None
    out: dict = {}

    async def _beat():
        while True:
            await asyncio.sleep(BEAT_SECONDS)
            await _put_state(report_id, {**state, "beat_at": _now().isoformat()})
    beat = asyncio.create_task(_beat())
    try:
        async with AsyncSessionLocal() as s:
            if save_usage:
                with cma_claude.save_usage(cma_save_usage.cap_minutes(config)):
                    out = await _generate_maybe_as_of(s, owner_id, report_id, tier, opts)
            else:
                out = await _generate_maybe_as_of(s, owner_id, report_id, tier, opts)
        if not (isinstance(out, dict) and out.get("ok")):
            err = "The AI couldn't read the photos this time. Answer the questions yourself, or try again."
    except AIRateLimitExceeded as e:
        err = e.user_message
    except HTTPException as e:
        err = str(e.detail)
    except asyncio.CancelledError:
        beat.cancel()
        await _put_state(report_id, {**state, "status": "failed", "finished_at": _now().isoformat(),
                                     "error": "Reading the photos stopped partway. Try again, or answer the questions yourself."})
        await release_cma_generation(report_id)
        raise
    except Exception:  # noqa: BLE001
        logger.exception("Seller photo read failed", extra={"report_id": report_id})
        err = "The AI couldn't read the photos this time. Answer the questions yourself, or try again."
    beat.cancel()
    try:
        await beat
    except BaseException:  # noqa: BLE001 - cancelled on purpose
        pass
    done = {**state, "status": "failed" if err else "ready", "finished_at": _now().isoformat(), "error": err}
    if not err:
        done.update(suggest=out.get("suggest") or {}, cost_usd=out.get("cost_usd"), reused=bool(out.get("reused")))
    # the run's hold is given back BEFORE the page can see "ready" - a press right after it never finds it still held
    try:
        await release_cma_generation(report_id)
    finally:
        await _put_state(report_id, done)
    if save_usage:
        await _tell(owner_id, report_id, address, err)


async def _tell(owner_id: str, report_id: str, address: str, err: Optional[str]) -> None:
    """The slower line can take several minutes: the bell says when the answers are ready to check."""
    try:
        from app.database import AsyncSessionLocal
        from app.models.database import Notification
        async with AsyncSessionLocal() as db:
            db.add(Notification(
                agent_id=owner_id, type="CMA_AI_FAILED" if err else "CMA_AI_READY", reference_type="CMA", reference_id=report_id,
                title=(f"Couldn't read the photos: {address}" if err else f"The AI read the photos: {address}")[:200],
                message=(err or "Check its answers on the Compare step, then make the report.")[:500],
                link=f"/studio/cma/{report_id}", is_read=False, created_at=_now()))
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("Seller photo read notice failed", exc_info=True, extra={"report_id": report_id})


# ---- Update the value (owner 11/19): an answer changed on the finished seller report -------------------------------
_AI_WRITTEN_PRICING_TEXT = ("pricing_commentary", "listing_strategy_notes", "listing_prep_notes")
_COMP_AI_FIELDS = ("quality", "ai_quality_read", "ai_commentary", "strength", "material_issues")


async def prepare_update(db, report, comp_marks: Optional[dict], kitchen: Optional[str], baths: Optional[str]) -> None:
    """The agent changed one or more answers on a finished seller report. Saves the new answers (they are the agent's
    now, not the photos'), takes back ONLY what the report's AI wrote (the agent's own edits stay), and marks the
    report so the next run re-makes it from the same photo read - the photos are never read again. The caller then
    starts the run like Create AI Analysis does. Raises a plain-English HTTPException when it can't."""
    from fastapi import HTTPException
    from app.services.cma_ai_service import (_COMP_MARKS, _has_completed_ai_pricing, _is_blank, _reasoning_cache_get,
                                             _seller_read_key)
    if report.mode == "buyer":
        raise HTTPException(status_code=400, detail="Changing the answers is for a seller report.")
    if not (getattr(report, "ai_analysis", None) and _has_completed_ai_pricing(report)):
        raise HTTPException(status_code=400, detail="Make the report first - then you can change an answer.")
    if report.generation_status == "generating":
        raise HTTPException(status_code=409, detail="This report is being made right now.")
    if not isinstance(await _reasoning_cache_get(db, _seller_read_key(report.id)), dict):
        raise HTTPException(status_code=409, detail="This report's photo read wasn't kept, so its answers can't be "
                                                    "changed here. Start a new CMA for this home to use different answers.")
    subj = dict(report.subject or {})
    sa = dict(subj.get("seller_answers") or {})
    marks = {str(k): str(v) for k, v in (sa.get("comp_marks") or {}).items()}
    ai_set = {str(x) for x in (sa.get("ai_filled") or [])}
    on_report = set(included_comp_keys(report.comparables))
    changed = False
    for k, v in (comp_marks or {}).items():
        k, v = str(k), str(v)
        if k not in on_report or v not in _COMP_MARKS:
            raise HTTPException(status_code=422, detail="That answer isn't for one of this report's comps.")
        if marks.get(k) != v:
            marks[k], changed = v, True
            ai_set.discard(k)
    for room, val in (("kitchen", kitchen), ("baths", baths)):
        if val is None:
            continue
        if val not in _ROOM_STATES:
            raise HTTPException(status_code=422, detail="Pick original, updated or fully redone.")
        if sa.get(room) != val:
            sa[room], changed = val, True
            ai_set.discard(room)
    if not changed:
        raise HTTPException(status_code=400, detail="Nothing changed - the report already uses these answers.")
    sa.update(comp_marks=marks, ai_filled=sorted(ai_set), update_pending=True)
    sa["ai_unread"] = [k for k in (sa.get("ai_unread") or []) if k not in marks]
    sa["ai_rooms_missing"] = [r for r in (sa.get("ai_rooms_missing") or []) if sa.get(r) in (None, "", "not_sure")]
    report.subject = {**subj, "seller_answers": sa}
    # take back what the AI wrote; what the agent typed over it stays
    filled = (report.ai_analysis or {}).get("seller_filled_pricing") or {}
    pricing = {k: v for k, v in (report.pricing or {}).items() if k != "ai_estimate"}
    for k, v in filled.items():
        if pricing.get(k) == v:
            pricing.pop(k, None)
    # The written sections are always the AI's: the agent's own wording for them is kept apart (presentation.text,
    # the Review step) and is untouched. Left in place they would state the OLD price next to the new value.
    for k in _AI_WRITTEN_PRICING_TEXT:
        pricing.pop(k, None)
    report.pricing = pricing
    comps = []
    for c in report.comparables or []:
        c = dict(c)
        if not _is_blank(c.get("commentary")) and c.get("commentary") == c.get("ai_commentary") and not c.get("commentary_by_agent"):
            c["commentary"] = None
        for f in _COMP_AI_FIELDS:
            c.pop(f, None)
        comps.append(c)
    report.comparables = comps
    report.ai_analysis = None
    await db.commit()
    await db.refresh(report)


def preview_update(report, comp_marks: Optional[dict]) -> dict:
    """What a changed answer would do to the value, BEFORE the agent presses Update the value (owner 11/20 - one answer
    can move it 10-15%). Free: the same placement rule the report used (cma_ai_service._seller_placement_check) run on
    this report's own adjusted sales with the new answers. The finished report may land up to 3% either side of it."""
    from app.services import cma_ai_service as ai
    if getattr(report, "mode", None) == "buyer":
        return {"ok": False}
    est = ((report.pricing or {}).get("ai_estimate") or {})
    anchor = ((est.get("valuation_trace") or {}).get("anchor")) or {}
    subj = dict(report.subject or {})
    sa = dict(subj.get("seller_answers") or {})
    marks = {**{str(k): str(v) for k, v in (sa.get("comp_marks") or {}).items()},
             **{str(k): str(v) for k, v in (comp_marks or {}).items() if str(v) in ai._COMP_MARKS}}
    pc = ai._seller_placement_check({**subj, "seller_answers": {**sa, "comp_marks": marks}}, anchor, report.mode)
    now = ai._as_float((report.pricing or {}).get("most_likely_value")) or ai._as_float(est.get("value"))
    if not pc or not now:
        return {"ok": False}
    about = int(round(pc["target"] / 5000.0) * 5000)
    return {"ok": True, "now": int(now), "about": about, "low": int(round(pc["allowed_low"], -3)), "high": int(round(pc["allowed_high"], -3)),
            "change_pct": round((about / now - 1) * 100, 1)}
