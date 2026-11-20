import asyncio
import logging
from typing import List

from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

logger = logging.getLogger(__name__)
from app.models.database import User
from app.schemas.cma import (
    CmaReportResponse, CmaReportListItem, CmaReportCreate, CmaReportUpdate,
    CmaCompSearchRequest, CmaCompCandidate, CmaShareResponse, CmaPublicResponse,
    CmaGenerateAiAnalysisRequest, CmaThemeUpdate,
)
from app.services import cma_ai_service, cma_service as service, cma_time_machine
from app.services.ai.ai_rate_limit_service import (
    AIRateLimitExceeded, CmaGenerationAlreadyRunning, hold_cma_generation, release_cma_generation,
)
from app.utils.auth import require_mls_access, require_broker_authorization

# No router-level auth dependency (unlike open-house-manager) because this router also serves
# the unauthenticated public share-link route below - each protected route declares its own
# require_basic_plan + require_broker_authorization instead.
router = APIRouter(prefix="/api/cma", tags=["cma"])

# Background AI runs in this process (kept referenced so they're never garbage-collected mid-run).
_BACKGROUND: set = set()


@router.get("", response_model=List[CmaReportListItem])
async def list_cma_reports(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    return await service.list_reports(db, current_user.id)


@router.post("", response_model=CmaReportResponse, status_code=201)
async def create_cma_report(
    payload: CmaReportCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    return await service.create_report(db, current_user.id, payload)


@router.post("/comparables/search", response_model=List[CmaCompCandidate])
async def search_cma_comparables(
    payload: CmaCompSearchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """Suggests candidate comparables from the local MLS mirror around a location - the agent
    decides what actually gets added to the CMA, this just narrows the search."""
    from app.services import cma_backtest_arms as _arms
    arm = await _arms.find_arm(db, current_user.id, listing_key=payload.listing_key,
                               address=_arms.subject_address(payload.listing_key, payload.subject))
    if arm:   # CMA accuracy test only: the search window ends on the as-of date
        payload = _arms.shift_search_window(payload, arm)
    with _arms.activated(arm):
        results = await service.search_comparables(db, payload)
    # For a listed home the Match % of each sale includes its price level (cma_comp_pick.apply_price_level)
    from app.services import cma_comp_pick
    _subj = payload.subject.model_dump() if payload.subject else {}
    return cma_comp_pick.apply_price_level(_subj, results, asking_price=_subj.get("list_price"), subject_sqft=payload.subject_sqft)


@router.post("/comparables/auto-search")
async def auto_search_cma_comparables(
    payload: CmaCompSearchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """The comp step's first search: picks its own radius and sold window (time first, then
    distance, never past this product's limit) and says when comps are limited - see
    cma_service.auto_search_comparables. Manual changes after that use /comparables/search."""
    from app.services import cma_backtest_arms as _arms
    arm = await _arms.find_arm(db, current_user.id, listing_key=payload.listing_key,
                               address=_arms.subject_address(payload.listing_key, payload.subject))
    if arm:
        payload = _arms.shift_search_window(payload, arm)
    with _arms.activated(arm):
        out = await service.auto_search_comparables(db, payload)
    # For a listed home the Match % of each sale includes its price level (cma_comp_pick.apply_price_level), so
    # the list still runs highest to lowest and the Comps step still picks the highest.
    from app.services import cma_comp_pick
    _subj = payload.subject.model_dump() if payload.subject else {}
    results = cma_comp_pick.apply_price_level(_subj, out["results"], asking_price=_subj.get("list_price"),
                                              subject_sqft=payload.subject_sqft)
    return {**out, "results": [c.model_dump() for c in results]}


@router.get("/public/{slug}", response_model=CmaPublicResponse)
async def get_public_cma_report(
    slug: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Unauthenticated - this is the client-facing share link, no login required."""
    response.headers["Cache-Control"] = "no-store"
    return await service.get_public_report(db, slug)


@router.get("/map/{ref}.png")
async def cma_location_map(
    ref: str,
    m: str = Query(..., min_length=5, max_length=600),
    size: str = Query("900x420"),
    z: Optional[int] = Query(None, ge=14, le=18),
    db: AsyncSession = Depends(get_db),
):
    """Unauthenticated - the PDF (Studio and the client share link) fetches it without a login. `ref` is the
    report id or its share slug; every pin in `m` must be that report's subject or one of its comps, so this
    only ever draws a report's own map (server Google key, cached per pin set)."""
    from sqlalchemy import or_, select as _select
    from app.models.database import CmaReport
    from app.services import cma_manual_subject_service as manual
    try:
        pins = manual.parse_map_pins(m)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Bad map pins.")
    if size not in manual.CMA_MAP_SIZES:
        raise HTTPException(status_code=422, detail="Bad map size.")
    if z is not None and len(pins) != 1:
        raise HTTPException(status_code=422, detail="Bad map pins.")
    report = (await db.execute(_select(CmaReport).where(or_(CmaReport.id == ref, CmaReport.share_slug == ref)))).scalars().first()
    if not report or not manual.map_pins_belong(pins, report.subject or {}, report.comparables or []):
        raise HTTPException(status_code=404, detail="Map not found.")
    png = await manual.cma_map_png(db, size, pins, zoom=z)
    if not png:
        raise HTTPException(status_code=503, detail="Map unavailable.")
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/ai-estimate")
async def cma_ai_estimate(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
):
    """What one "Create AI Analysis" usually costs, as a share of this agent's monthly AI allowance -
    shown by the button before they spend it (user, 10/17). Typical = the average real cost of recent
    runs across the platform (photo review + reasoning); "up to" = the per-run cap."""
    from sqlalchemy import func, select
    from app.models.database import AIUsageEvent
    from app.services.ai.ai_config_service import ai_config_service
    config = await ai_config_service.get_config(db)
    from app.services.ai.ai_rate_limit_service import monthly_limit_usd
    limit = await monthly_limit_usd(db, current_user.id, config, current_user.plan_tier)
    cap = float(config.get("cma_ai_max_cost_per_generation_usd") or 0.75)

    async def avg(kind: str) -> float:
        sub = (select(AIUsageEvent.estimated_cost_usd).where(AIUsageEvent.event_type == kind, AIUsageEvent.blocked.is_(False),
                                                             AIUsageEvent.estimated_cost_usd > 0)
               .order_by(AIUsageEvent.created_at.desc()).limit(40)).subquery()
        return float((await db.execute(select(func.avg(sub.c.estimated_cost_usd)))).scalar() or 0)

    # Made the normal way: the real cost of recent finished reports (11/17 - the usage log mixes in half-price
    # answers once Save usage runs exist, so whole reports are read instead); the usage log until there are some.
    from sqlalchemy import text as _text
    from app.services import cma_save_usage

    async def report_costs(save: bool) -> list:
        rows = (await db.execute(_text(
            "select (ai_analysis->>'cost_usd')::float, (ai_analysis->'save_usage'->>'minutes')::float from cma_reports "
            "where ai_analysis is not null and is_backtest is false and (ai_analysis->>'cost_usd')::float > 0 "
            # a run picked back up after a restart only knows part of its cost and time - left out
            "and (ai_analysis ? 'save_usage') = :save and coalesce(ai_analysis->'save_usage'->>'resumed', 'false') <> 'true' "
            "order by (ai_analysis->>'generated_at') desc nulls last limit 20"),
            {"save": save})).all()
        return [(float(r[0]), r[1]) for r in rows]

    now_rows = await report_costs(False)
    typical = sum(r[0] for r in now_rows) / len(now_rows) if len(now_rows) >= 3 else (await avg("cma_ai")) + (await avg("cma_reasoning"))
    if typical <= 0:
        typical = cap * 0.6
    typical = min(typical, cap)
    save_rows = await report_costs(True)
    save_usd = sum(r[0] for r in save_rows) / len(save_rows) if len(save_rows) >= 3 else typical / 2
    minutes = [r[1] for r in save_rows[:5] if r[1]]
    pct = (lambda usd: round(usd / float(limit) * 100, 1) if limit else None)
    # "usd"/"percent"/"max_percent" stay as they were (the made-now figure) for anything already reading them
    return {"usd": round(typical, 3), "percent": pct(typical), "max_percent": pct(cap),
            "now": {"usd": round(typical, 3), "percent": pct(typical), "minutes": cma_save_usage.NOW_MINUTES},
            # at best every step goes through the slower line; at worst the 20 minutes run out and the rest is made the
            # normal way - never more than a report made now
            "save_usage": {"usd": round(save_usd, 3), "percent": pct(save_usd), "usd_max": round(typical, 3), "percent_max": pct(typical),
                           **cma_save_usage.times(config, sum(minutes) / len(minutes) if minutes else None)}}


# ---- Manual subject (a home not on the MLS yet) - see cma_manual_subject_service ----

@router.get("/subject-area")
async def cma_subject_area(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """School district / township / water / sewer most nearby MLS homes share, in the MLS's own
    spelling - pre-fills a manually entered subject so it matches comps the way an MLS one does."""
    from app.services import cma_manual_subject_service
    return await cma_manual_subject_service.area_suggestions(db, lat, lng)


@router.get("/subject-from-listing/{listing_key}")
async def cma_subject_from_listing(
    listing_key: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """The full CMA subject for an MLS listing, straight from our Bright record - location, school
    district, township, county, lot, basement, garage, water/sewer, taxes, style, levels, the
    listing description and every photo. Used when an agent picks one of their own listings or a
    searched address (10/20: picking "My listing" filled only address, price, beds/baths/sqft and a
    photo, so the home "wasn't on the map" and most facts were blank)."""
    subject = await service.subject_from_listing(db, listing_key)
    if not subject:
        raise HTTPException(status_code=404, detail="That listing isn't in our MLS data yet.")
    return subject


@router.get("/locate")
async def cma_locate_address(
    address: str = Query(..., min_length=5, max_length=300),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """Typed address -> location, for when Google's suggestions don't load in the browser."""
    from app.services import cma_manual_subject_service
    found = await cma_manual_subject_service.geocode_address(address)
    if not found:
        raise HTTPException(status_code=404, detail="Couldn't find that address on the map - check the spelling and ZIP.")
    return found


@router.post("/{report_id}/subject-photo")
async def upload_cma_subject_photo(
    report_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """A photo of a manually entered subject (JPG, PNG or WebP - stored as JPG, which the PDF needs)."""
    from app.services import cma_manual_subject_service as manual
    from app.utils.image_validation import photo_to_jpeg
    await service._get_owned(db, current_user.id, report_id)
    contents = await file.read()
    if len(contents) > manual.SUBJECT_PHOTO_MAX_BYTES:
        raise HTTPException(status_code=422, detail="Photo must be 10MB or smaller.")
    try:
        jpeg = photo_to_jpeg(contents)
    except ValueError:
        raise HTTPException(status_code=422, detail="Please upload a JPG, PNG or WebP photo.")
    return {"photo_url": await manual.save_subject_photo(db, current_user, jpeg)}


class CmaGooglePhotoRequest(BaseModel):
    kind: Literal["street_view", "aerial"] = "street_view"
    latitude: Optional[float] = None
    longitude: Optional[float] = None


@router.post("/{report_id}/subject-photo/google")
async def google_cma_subject_photo(
    report_id: str,
    payload: CmaGooglePhotoRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """Street View photo (or an aerial view) of the subject's location, saved as its photo."""
    from app.services import cma_manual_subject_service as manual
    report = await service._get_owned(db, current_user.id, report_id)
    subject = report.subject or {}
    lat = payload.latitude if payload.latitude is not None else subject.get("latitude")
    lng = payload.longitude if payload.longitude is not None else subject.get("longitude")
    if lat is None or lng is None:
        raise HTTPException(status_code=422, detail="Pick the address first - the photo is taken at its location.")
    try:
        jpeg = await manual.google_photo(float(lat), float(lng), payload.kind)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"photo_url": await manual.save_subject_photo(db, current_user, jpeg), "kind": payload.kind}


@router.get("/{report_id}", response_model=CmaReportResponse)
async def get_cma_report(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    return await service.get_report(db, current_user.id, report_id)


@router.patch("/{report_id}", response_model=CmaReportResponse)
async def update_cma_report(
    report_id: str,
    payload: CmaReportUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    return await service.update_report(db, current_user.id, report_id, payload)


@router.patch("/{report_id}/theme", status_code=204)
async def update_cma_default_theme(
    report_id: str,
    payload: CmaThemeUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    await service.update_default_theme(db, current_user.id, report_id, payload.default_theme)


@router.delete("/{report_id}", status_code=204)
async def delete_cma_report(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    await service.delete_report(db, current_user.id, report_id)


async def _generate_maybe_as_of(db, owner_id: str, report_id: str, tier, opts: dict):
    """The normal generation. Only for an accuracy-test report with a live arm (see
    cma_backtest_arms) it runs inside the time machine - in a dry run with zero-cost AI stand-ins."""
    from app.services import cma_backtest_arms as _arms
    arm = await _arms.find_arm(db, owner_id, report_id=report_id)
    if not arm:
        return await cma_ai_service.generate_ai_analysis(db, owner_id, report_id, plan_tier=tier, **opts)
    with _arms.activated(arm) as tm:
        if arm.dry_run:
            from app.services.cma_backtest_stubs import StubAnthropic, stub_reasoning
            rep = await service.get_report(db, owner_id, report_id)
            tm.fake_client = StubAnthropic({"list_price": (arm.facts or {}).get("list_price"), "comps": [
                {"listing_key": c.get("listing_key"), "address": c.get("street_address"), "price": c.get("sale_price") or c.get("list_price")}
                for c in (rep.comparables or []) if c.get("is_included", True)],
                **((arm.facts or {}).get("stub") or {})})
            tm.fake_reasoning = stub_reasoning
            opts = {**opts, "include_map_images": False}   # the one paid Google call - off in a dry run
        try:
            return await cma_ai_service.generate_ai_analysis(db, owner_id, report_id, plan_tier=tier, **opts)
        finally:
            for i, cap in enumerate(tm.captured):
                cma_time_machine.check_payload(cap, f"captured model request {i + 1} ({cap.get('provider')})", raise_now=False)
            arm.facts = {**(arm.facts or {}), "leak_checks": tm.checks, "leak_log": tm.leak_log,
                         "captured_requests": len(tm.captured), "ai_input_excerpt": tm.prompt_excerpt,
                         # which AI steps each run asked for, run after run (the seller photo read is reused, 11/19)
                         "captured_tools": [*((arm.facts or {}).get("captured_tools") or []),
                                            [",".join(c.get("tool") or []) or ("text" if c.get("provider") == "anthropic" else str(c.get("provider")))
                                             for c in tm.captured]]}
            await db.commit()


# ---------------------------------------------------------------------------------------------------
# A background run, and picking one back up after a restart (owner 11/17: "a blog in Save usage mode picks itself
# back up after a restart; CMAs don't - easy fix? do it"). Every push restarts the server; a run cut off by it used
# to be lost and the agent had to press the button again. Now a live run renews a shared marker every minute
# (cluster lease "cma_run"); the every-2-minutes sweep (resume_stalled, main.py) finds a run still marked
# "generating" with no live marker and runs it again from the start - the same as the agent pressing the button,
# within its own time limit (a Save usage run keeps the time it has already used: it never goes past ~25 minutes
# in all). Only runs the button / the AI helper started (generation_mode set) are ever picked up.
# ---------------------------------------------------------------------------------------------------
_RUN_LEASE = "cma_run"
_RUN_LEASE_TTL = 180            # renewed every minute; a restart lets it lapse within 3 minutes
_RESUME_AFTER_SECONDS = 240     # a run this young is never taken for dead
_FAST_RUN_SECONDS = 8 * 60      # room a whole report needs the normal way (about 3 minutes; 8 to be safe)
_RESUME_OPTS = dict(max_photos_per_property=None, include_map_images=True, force_regenerate=False)   # what the page asks for
_LOCAL_RUNS: set = set()        # report ids being made in THIS copy right now
CUT_OFF_MESSAGE = "That run was cut off by a site update and there wasn't time left to finish it. Press the button to make it again."


def _start_run(report_id: str, owner_id: str, tier, opts: dict, save_usage: bool, config: dict, address: str,
               began, *, run_token: Optional[str] = None, resumed: bool = False) -> None:
    task = asyncio.create_task(_run_generation(report_id, owner_id, tier, opts, save_usage, config, address, began,
                                               run_token=run_token, resumed=resumed))
    _LOCAL_RUNS.add(report_id)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    task.add_done_callback(lambda _t: _LOCAL_RUNS.discard(report_id))


async def _run_generation(report_id: str, owner_id: str, tier, opts: dict, save_usage: bool, config: dict, address: str,
                          began, *, run_token: Optional[str] = None, resumed: bool = False) -> None:
    """One background run (the button / the AI helper, or the sweep picking one back up). The caller has taken
    the allowance hold (hold_cma_generation) and marked the report "generating"; this always gives both back."""
    from datetime import datetime as _dt, timezone as _tz
    from app.database import AsyncSessionLocal
    from app.services import cluster, cma_claude, cma_save_usage
    ok, err, cost, beat = False, None, None, None
    try:
        if run_token is None:
            run_token = await cluster.acquire(_RUN_LEASE, report_id, _RUN_LEASE_TTL)
        if run_token:
            beat = cluster.start_keep_alive(_RUN_LEASE, report_id, run_token, _RUN_LEASE_TTL)
    except Exception:  # noqa: BLE001 - the run itself matters more than the marker
        logger.warning("CMA run marker not taken", exc_info=True, extra={"report_id": report_id})
    try:
        async with AsyncSessionLocal() as s:
            await service.drop_subject_from_comps(s, owner_id, report_id)
            if save_usage:
                # the slower line's time is counted from when the agent pressed the button - a run picked back up
                # after a restart only has what is left of it (none left: the rest is made the normal way)
                left = cma_save_usage.cap_minutes(config) - (_dt.now(_tz.utc) - began).total_seconds() / 60
                with cma_claude.save_usage(max(0.0, left)) as su:
                    rep = await _generate_maybe_as_of(s, owner_id, report_id, tier, opts)
                # how long it took and how much went through the slower line - later reports' "likely
                # ready by" is measured from these
                rep.ai_analysis = {**(rep.ai_analysis or {}),
                                   "save_usage": {**cma_save_usage.run_record(su, began), **({"resumed": True} if resumed else {})}}
                # a run picked back up after a restart only knows its own part of the cost: no "used about X%" line
                cost = None if resumed else (float((rep.ai_analysis or {}).get("cost_usd") or 0) or None)
                await s.commit()
            else:
                await _generate_maybe_as_of(s, owner_id, report_id, tier, opts)
        ok = True
    except AIRateLimitExceeded as e:
        err = e.user_message
    except HTTPException as e:
        err = str(e.detail)
    except asyncio.CancelledError:
        # the server is shutting down (a push): leave it marked "generating" - the sweep picks it back up
        if beat:
            beat.cancel()
        raise
    except Exception:
        logger.exception("CMA AI analysis failed unexpectedly (background)", extra={"report_id": report_id})
        err = "The AI analysis hit an unexpected error partway through - please try again."
        try:
            from app.services.admin_alerts import alert_admin
            await alert_admin("cma", "CMA AI analysis crashed", f"report {report_id}", owner_id=owner_id)
        except Exception:  # noqa: BLE001
            pass
    try:
        await service.mark_generation_finished(owner_id, report_id, "complete" if ok else "failed", error=err)
    finally:
        await release_cma_generation(report_id)
        if beat:
            beat.cancel()
        if run_token:
            try:
                await cluster.release(_RUN_LEASE, report_id, run_token)
            except Exception:  # noqa: BLE001 - it lapses by itself
                pass
        await _tell(owner_id, report_id, address, save_usage, ok, err, cost)


async def _tell(owner_id: str, report_id: str, address: str, save_usage: bool, ok: bool, err: Optional[str], cost) -> None:
    from app.services import cma_save_usage
    if save_usage:
        # the agent chose to walk away: the bell and an email, every time
        await cma_save_usage.tell_agent(owner_id, report_id, address, None if ok else err, cost)
    # Started as an AI CMA from the helper's chat? Its card and chat hear about it
    # (no-op otherwise - ai_notify only answers what it was asked to watch).
    try:
        from app.services.ai import ai_notify
        await ai_notify.finished(
            owner_id, f"cma:{report_id}", ok,
            "Your AI CMA for {label} is finished - comps picked, value estimated and the report written. Take a look before you share it."
            if ok else f"The AI CMA for {{label}} didn't finish: {err or 'something went wrong'} Open it to try again or pick the comps yourself.",
            link=f"/studio/cma/{report_id}", link_label="Open the CMA")
    except Exception:  # noqa: BLE001
        logger.warning("CMA helper notice failed", exc_info=True, extra={"report_id": report_id})


async def resume_stalled() -> int:
    """Scheduler job (main.py, every 2 minutes): CMA runs the button started that are still marked "generating"
    with no live run in ANY copy of the API - cut off by a restart - start again by themselves. Returns how many
    this copy took."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.database import CmaReport
    from app.services import cluster
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(CmaReport.id, CmaReport.owner_id, CmaReport.generation_started_at).where(
                CmaReport.generation_status == "generating",
                CmaReport.generation_mode.in_(("now", "save_usage")),
                CmaReport.generation_started_at < _dt.now(_tz.utc) - _td(seconds=_RESUME_AFTER_SECONDS)))).all()
        busy = await cluster.alive_keys(_RUN_LEASE, [r[0] for r in rows])
    except Exception:  # noqa: BLE001
        logger.warning("CMA resume sweep failed", exc_info=True)
        return 0
    n = 0
    for rid, owner_id, started in rows:
        if rid in _LOCAL_RUNS or rid in busy:
            continue
        try:
            n += await _resume_one(rid, owner_id, started)
        except Exception:  # noqa: BLE001
            logger.warning("CMA resume failed", exc_info=True, extra={"report_id": rid})
    if n:
        logger.info("CMA runs picked back up after a restart", extra={"count": n})
    return n


async def _resume_one(rid: str, owner_id: str, started) -> int:
    from datetime import datetime as _dt, timezone as _tz
    from app.database import AsyncSessionLocal
    from app.models.database import CmaReport, User
    from app.services import cluster
    from app.services.ai.ai_config_service import ai_config_service
    from app.services.cma_ai_service import _has_completed_ai_pricing
    token = await cluster.acquire(_RUN_LEASE, rid, _RUN_LEASE_TTL)
    if token is None:
        return 0   # some copy has it right now
    handed = False
    try:
        async with AsyncSessionLocal() as db:
            rep = await db.get(CmaReport, rid)
            if rep is None or rep.generation_status != "generating" or rep.generation_started_at != started \
                    or rep.generation_mode not in ("now", "save_usage"):
                return 0   # it finished, failed or started again while we looked
            save = rep.generation_mode == "save_usage"
            subj = rep.subject or {}
            address = str(subj.get("street_address") or subj.get("address") or "your home")
            done = bool(rep.ai_analysis) and _has_completed_ai_pricing(rep)
            cost = float((rep.ai_analysis or {}).get("cost_usd") or 0) or None
            limit = service._stale_after(rep)
            user = await db.get(User, owner_id)
            config = await ai_config_service.get_config(db)
            began = started if started.tzinfo else started.replace(tzinfo=_tz.utc)
            if done:
                # it finished - only the "finished" mark was lost with the restart
                await service.mark_generation_finished(owner_id, rid, "complete")
                await _tell(owner_id, rid, address, save, True, None, cost)
                return 1
            if user is None or (_dt.now(_tz.utc) - began).total_seconds() > limit - _FAST_RUN_SECONDS:
                await service.mark_generation_finished(owner_id, rid, "failed", error=CUT_OFF_MESSAGE)
                await _tell(owner_id, rid, address, save, False, CUT_OFF_MESSAGE, None)
                return 1
            # its allowance hold went with the old copy: take it again (the old shared one first - it was this run's)
            await cluster.release("cma_hold", rid)
            try:
                await hold_cma_generation(db, owner_id, rid, config, user.plan_tier)
            except CmaGenerationAlreadyRunning:
                return 0
            except AIRateLimitExceeded as e:
                await service.mark_generation_finished(owner_id, rid, "failed", error=e.user_message)
                await _tell(owner_id, rid, address, save, False, e.user_message, None)
                return 1
            tier = user.plan_tier
        logger.info("CMA run picked back up after a restart", extra={"report_id": rid, "save_usage": save})
        try:
            _start_run(rid, owner_id, tier, dict(_RESUME_OPTS), save, config, address, began, run_token=token, resumed=True)
        except BaseException:
            await release_cma_generation(rid)
            raise
        handed = True
        return 1
    finally:
        if not handed:
            await cluster.release(_RUN_LEASE, rid, token)


class SellerPrefillRequest(BaseModel):
    save_usage: bool = True


@router.get("/{report_id}/seller-prefill")
async def get_seller_prefill(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """Seller Compare step (11/19): where the AI's read of the photos stands - none / reading / ready (with the answers
    it suggests) / failed - and whether the comps or photos changed since (stale). See cma_seller_prefill."""
    from app.services import cma_seller_prefill as pf
    rep = await service.get_report(db, current_user.id, report_id)     # 404 for someone else's report
    return pf.public_state(rep, await pf.get_state(db, report_id))


@router.post("/{report_id}/seller-prefill")
async def start_seller_prefill(
    report_id: str,
    payload: SellerPrefillRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """Seller Compare step: the AI reads the photos and suggests the answers. The report reuses this read (no second
    look at the photos). Save usage = the slower, half-price line, with a bell when it is done."""
    from app.services import cma_seller_prefill as pf
    from app.services.ai.ai_config_service import ai_config_service
    rep = await service.get_report(db, current_user.id, report_id)
    return await pf.start(db, current_user, rep, await ai_config_service.get_config(db), payload.save_usage)


@router.post("/{report_id}/generate-ai-analysis", response_model=CmaReportResponse)
async def generate_cma_ai_analysis(
    report_id: str,
    payload: CmaGenerateAiAnalysisRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """Writes a short AI-drafted note per comp (ai_commentary) and a proposed value estimate
    (pricing.ai_estimate) - never overwrites the agent's own commentary/pricing, see
    cma_ai_service.py."""
    _existing = await service.get_report(db, current_user.id, report_id)   # 404 for someone else's report
    # A report that already has its analysis is refused HERE, before anything is marked. The background run
    # used to mark it "generating", fail on the same rule a moment later, and leave a finished, shared report
    # showing "failed" (524 Wales Rd, test round 11/14 - a second request from a stale tab or a double click).
    from app.services.cma_ai_service import _has_completed_ai_pricing
    if getattr(_existing, "ai_analysis", None) and _has_completed_ai_pricing(_existing):
        raise HTTPException(status_code=400, detail="This report already has an AI analysis - regenerating isn't available.")
    # Start gate (before anything is marked or spent): one run per report, and the agent's other
    # in-flight runs count against their allowance - see ai_rate_limit_service.hold_cma_generation.
    # Refused here, the other run's 'generating' marker is left alone.
    from app.services.ai.ai_config_service import ai_config_service
    _config = await ai_config_service.get_config(db)
    try:
        await hold_cma_generation(db, current_user.id, report_id, _config, current_user.plan_tier)
    except CmaGenerationAlreadyRunning as e:
        raise HTTPException(status_code=409, detail=e.user_message)
    except AIRateLimitExceeded as e:
        raise HTTPException(status_code=429, detail=e.user_message)

    save_usage = bool(payload.save_usage)
    if payload.background or save_usage:
        # Runs on its own session, untied to this request: the answer comes back at once
        # ('generating') and the page polls GET /{id} until it's 'complete' or 'failed' (with
        # generation_error saying why). Nothing the network does can break it any more.
        # Save usage (11/17): always this way - the same run through the slower, half-price line (cma_save_usage).
        try:
            claimed = await service.mark_generation_started(db, current_user.id, report_id, claim=True,
                                                            mode="save_usage" if save_usage else "now")
        except BaseException:
            await release_cma_generation(report_id)
            raise
        if not claimed:
            # Another server copy is already running this report (11/14 - several copies): leave it be.
            await release_cma_generation(report_id)
            raise HTTPException(status_code=409, detail=CmaGenerationAlreadyRunning.user_message)
        _subj = getattr(_existing, "subject", None) or {}
        address = str(_subj.get("street_address") or _subj.get("address") or "your home")
        opts = dict(max_photos_per_property=payload.max_photos_per_property, include_map_images=payload.include_map_images,
                    force_regenerate=payload.force_regenerate)
        from datetime import datetime as _dt, timezone as _tz
        try:
            _start_run(report_id, current_user.id, current_user.plan_tier, opts, save_usage, _config, address, _dt.now(_tz.utc))
        except BaseException:
            await release_cma_generation(report_id)
            raise
        return await service.get_report(db, current_user.id, report_id)

    success = False
    try:
        await service.drop_subject_from_comps(db, current_user.id, report_id)
        result = await _generate_maybe_as_of(db, current_user.id, report_id, current_user.plan_tier, dict(
            max_photos_per_property=payload.max_photos_per_property,
            include_map_images=payload.include_map_images,
            force_regenerate=payload.force_regenerate,
        ))
        success = True
        return result
    except AIRateLimitExceeded as e:
        raise HTTPException(status_code=429, detail=e.user_message)
    except HTTPException:
        raise
    except Exception:
        # A bug anywhere in the (long, paid) generation pipeline must come back as a readable
        # JSON error - otherwise it propagates through the logging middleware as a torn stream
        # and the browser only sees "Failed to fetch" with no way to tell what went wrong.
        logger.exception("CMA AI analysis failed unexpectedly", extra={"report_id": report_id})
        raise HTTPException(
            status_code=502,
            detail="The AI analysis hit an unexpected error partway through. Your usage for this run was still counted - try again, and if it keeps failing, tell support which CMA.",
        )
    finally:
        # Clears the in-flight marker regardless of how this exited (success, a rate limit, a
        # validation HTTPException, or an unexpected error) - a no-op if generation never actually
        # started (e.g. failed validation before mark_generation_started ran).
        try:
            await service.mark_generation_finished(current_user.id, report_id, "complete" if success else "failed")
        finally:
            await release_cma_generation(report_id)


class SellerUpdateRequest(BaseModel):
    comp_marks: dict = {}
    kitchen: Optional[str] = None
    baths: Optional[str] = None
    save_usage: bool = False


@router.post("/{report_id}/seller-update", response_model=CmaReportResponse)
async def seller_update_answers(
    report_id: str,
    payload: SellerUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """Update the value (owner 11/19): the agent changed an answer on a finished seller report. The new answers are
    saved, only what the AI wrote is taken back, and the report is re-made from the same photo read - the photos are
    not read again (cma_seller_prefill.prepare_update). Starts like Create AI Analysis; the page polls."""
    from app.services import cma_seller_prefill as pf
    import copy as _copy
    rep = await service.get_report(db, current_user.id, report_id)
    before = {f: _copy.deepcopy(getattr(rep, f)) for f in ("subject", "pricing", "comparables", "ai_analysis")}
    await pf.prepare_update(db, rep, payload.comp_marks, payload.kitchen, payload.baths)
    try:
        return await generate_cma_ai_analysis(
            report_id, CmaGenerateAiAnalysisRequest(include_map_images=True, background=True, save_usage=payload.save_usage),
            db=db, current_user=current_user, _broker=_broker)
    except HTTPException:
        # not started (the monthly AI allowance, another run going...): the finished report comes back as it was
        rep = await service.get_report(db, current_user.id, report_id)
        for f, v in before.items():
            setattr(rep, f, v)
        await db.commit()
        raise


@router.get("/{report_id}/adjusted-sales-preview")
async def cma_adjusted_sales_preview(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """Free, no AI: what the comparable sales point to with the comps as they stand now, next to the figure and the
    value the report still carries - for the editor after a comp is added or removed. Changes nothing in the report.
    See cma_ai_service.adjusted_sales_preview."""
    return await cma_ai_service.adjusted_sales_preview(db, current_user.id, report_id)


@router.post("/{report_id}/recompute-valuation", response_model=CmaReportResponse)
async def recompute_cma_valuation(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """Re-runs the deterministic evidence/confidence/value-drivers pipeline plus one lightweight
    (text-only, no photos) AI call to keep the value/methodology text consistent with whatever
    comp overrides the agent has made since the initial generation (strength reclassifications,
    Mark Irrelevant) - see cma_ai_service.recompute_valuation. Repeatable, unlike the one-time
    initial generation; gated by the same shared monthly AI budget as every other metered tool."""
    success = False
    try:
        result = await cma_ai_service.recompute_valuation(db, current_user.id, report_id, plan_tier=current_user.plan_tier)
        success = True
        return result
    except AIRateLimitExceeded as e:
        raise HTTPException(status_code=429, detail=e.user_message)
    finally:
        await service.mark_generation_finished(current_user.id, report_id, "complete" if success else "failed")


@router.post("/{report_id}/resync-qa", response_model=CmaReportResponse)
async def resync_cma_qa(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """Free re-stamp for a report whose only problem is a stale artifact hash (a save
    round-tripped after final QA with nothing about the content actually changing) - no AI call,
    no cost. Returns 409 if a real content finding is also present; that needs
    recompute-valuation or a full regenerate instead. See cma_service.resync_qa."""
    return await service.resync_qa(db, current_user.id, report_id)


@router.post("/{report_id}/share", response_model=CmaShareResponse)
async def share_cma_report(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
    _broker: User = Depends(require_broker_authorization),
):
    """Generates (or reuses) a public link. The agent keeps editing the same report afterward -
    no separate 'republish' step, the link always reflects the latest saved state."""
    return await service.share_report(db, current_user.id, report_id)
