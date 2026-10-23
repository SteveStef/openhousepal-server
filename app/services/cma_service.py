import os
import secrets
import string
import hashlib
import json
import re
from datetime import datetime, date, timedelta, timezone
from typing import Any, Dict, Optional, List

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.database import CmaReport, HomeType, Property, User, StudioBrandKit, AgentHub, Site, SiteDomain
from app.schemas.cma import (
    CmaReportCreate, CmaReportUpdate, CmaCompSearchRequest, CmaCompCandidate,
    CmaShareResponse, CmaPublicResponse, CmaPublicAgent, CmaReportListItem,
)
from app.services.website.domain_resolution_service import OHP_BASE_DOMAIN
from app.services.cma_similarity_service import (
    MATCH_DISPLAY_CALIBRATION_VERSION,
    calibrate_match_percentage,
    load_local_proximity_context,
)
from app.services.cma_artifact_service import saved_artifact_matches, stamp_final_artifact_qa
from app.utils.geo import distance_miles_sql, get_lat_long_offsets, haversine_distance, is_within_distance
from app.utils.listing_dom import dom_fetched_at, resolve_days_on_market


_AUTHORITATIVE_SUBJECT_FACT_FIELDS = (
    "above_grade_finished_area",
    "below_grade_finished_area",
    "architectural_style",
    "levels",
    "township",
    "subdivision_name",
    "latitude",
    "longitude",
    "sqft",
    "lot_size",
    "year_built",
    "property_type",
    "school_district",
    "public_remarks",
)


def _missing_subject_fact(value: Any) -> bool:
    return value is None or value == "" or value == []


def _mls_baths(prop: Property) -> Optional[float]:
    """Bathroom count, or None when the MLS doesn't give one. Bright sends no bath count on
    Multi-Family listings (only unit mixes), so every one arrives as 0 baths - 10/23 a duplex at
    1703 Point Breeze Ave showed 0 baths and the "beds with no baths" junk filter threw out every
    multi-family sale, so its comp search came back empty. Unknown, not zero."""
    baths = getattr(prop, "bathrooms", None)
    if not baths and getattr(prop, "mls_property_type", None) == "Multi-Family":
        return None
    return baths


def _property_subject_facts(prop: Property) -> Dict[str, Any]:
    """One MLS-backed source for hard subject facts used by every match calculation."""
    raw = getattr(prop, "raw_mls_data", None) or {}
    return {
        "above_grade_finished_area": prop.above_grade_finished_area,
        "below_grade_finished_area": prop.below_grade_finished_area,
        "architectural_style": prop.architectural_style,
        "levels": getattr(prop, "levels", None),
        "township": prop.township,
        "subdivision_name": prop.subdivision_name,
        "latitude": prop.latitude,
        "longitude": prop.longitude,
        "sqft": prop.living_area,
        "lot_size": prop.lot_size,
        "year_built": prop.year_built,
        "property_type": prop.home_type,
        "school_district": raw.get("SchoolDistrictName") or prop.school_district_name,
        "public_remarks": raw.get("PublicRemarks") or prop.description,
    }


def _join_mls_list(value: Any) -> Any:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v) or None
    return value


def _property_display_facts(prop: Property) -> Dict[str, Any]:
    """Subject facts the report shows and the engine adjusts on, but that aren't part of the Match %
    fingerprint. Filled from the MLS row when the saved subject lacks them: a 10/20 report for 417
    Dogwood Ln came in with no water, sewer, taxes or garage (the address lookup didn't return
    them), so the inspection page said the sewer type was unknown and the garage went unadjusted."""
    raw = getattr(prop, "raw_mls_data", None) or {}
    garage = None
    if getattr(prop, "has_garage", None) and getattr(prop, "garage_spaces", None):
        garage = f"{int(prop.garage_spaces)} Car"
    elif getattr(prop, "has_garage", None):
        garage = "Yes"
    elif getattr(prop, "has_garage", None) is False:
        garage = "None"
    basement = _join_mls_list(raw.get("Basement"))
    return {
        "water_source": _join_mls_list(raw.get("WaterSource") or getattr(prop, "water_source", None)),
        "sewer": _join_mls_list(raw.get("Sewer") or getattr(prop, "sewer", None)),
        "annual_taxes": raw.get("TaxAnnualAmount") or getattr(prop, "tax_annual_amount", None),
        "garage": garage,
        "basement": basement,
        "beds": getattr(prop, "bedrooms", None),
        "baths": _mls_baths(prop),
    }


async def subject_from_listing(db: AsyncSession, listing_key: str) -> Optional[Dict[str, Any]]:
    """Full CMA subject for one MLS listing from our mirror (see the /subject-from-listing route)."""
    prop = (await db.execute(select(Property).where(Property.listing_key == str(listing_key)))).scalar_one_or_none()
    if prop is None:
        return None
    from app.services.cma_backtest_service import map_property_to_subject
    from app.services.property_service import PropertyService
    found = await PropertyService.get_property_by_listing_key(db, str(listing_key))
    subject = map_property_to_subject(found.model_dump(by_alias=True)) if found else {"listing_key": str(listing_key), "source": "mls"}
    if not subject.get("baths") and _mls_baths(prop) is None:
        # Multi-Family listing (no bath count from Bright). The same house is often also filed as
        # a Residential listing that does carry baths - use that, else leave it for the agent.
        twin = (await db.execute(select(Property.bathrooms).where(
            Property.street_address == prop.street_address, Property.zipcode == prop.zipcode,
            Property.listing_key != prop.listing_key, Property.bathrooms > 0,
        ).order_by(Property.modification_timestamp.desc().nullslast()).limit(1))).scalar_one_or_none()
        subject["baths"] = twin
    subject = _merge_authoritative_subject_facts(subject, prop)
    raw = getattr(prop, "raw_mls_data", None) or {}
    extra = {
        "street_address": prop.street_address, "city": prop.city, "state": prop.state, "zipcode": prop.zipcode,
        "latitude": prop.latitude, "longitude": prop.longitude, "list_price": prop.price,
        "county": (raw.get("County") or "").replace("-PA", "").replace("-", " ").title() or None,
        "heating": _join_mls_list(raw.get("Heating")), "cooling": _join_mls_list(raw.get("Cooling")),
        "garage_spaces": getattr(prop, "garage_spaces", None), "stories": getattr(prop, "stories", None),
        "full_baths": raw.get("BathroomsFull"), "half_baths": raw.get("BathroomsHalf"),
        "photos": [u for u in (prop.photos or []) if isinstance(u, str)] or subject.get("photos") or [],
        "photo_url": getattr(prop, "img_src", None) or subject.get("photo_url"),
    }
    for k, v in extra.items():
        if _missing_subject_fact(subject.get(k)) and not _missing_subject_fact(v):
            subject[k] = v
    if prop.photos and len(prop.photos) > len(subject.get("photos") or []):
        subject["photos"] = [u for u in prop.photos if isinstance(u, str)]
    for k in ("water_source", "sewer", "heating", "cooling", "basement"):
        subject[k] = _join_mls_list(subject.get(k))
    # Heating fuel is its own Bright field (HeatingFuel, e.g. "Propane - Owned") - add it in the
    # form's own words so the Heating Fuel box isn't blank (10/20).
    fuel_text = (_join_mls_list(raw.get("HeatingFuel")) or "").lower()
    fuel = next((name for name, words in (("Natural Gas", ("natural gas", "gas")), ("Propane", ("propane",)),
                                          ("Oil", ("oil",)), ("Electric", ("electric",))) if any(w in fuel_text for w in words)), None)
    if fuel and fuel not in (subject.get("heating") or ""):
        subject["heating"] = ", ".join(x for x in (subject.get("heating"), fuel) if x)
    if isinstance(subject.get("garage"), str):
        subject["garage"] = re.sub(r"(\d+)\.0\b", r"\1", subject["garage"])
    if str(subject.get("subdivision_name") or "").strip().upper() in ("NONE AVAILABLE", "NONE", "N/A"):
        subject["subdivision_name"] = None
    from app.schemas.cma import CmaSubjectProperty
    return CmaSubjectProperty(**{k: v for k, v in subject.items() if k in CmaSubjectProperty.model_fields}).model_dump()


def _merge_authoritative_subject_facts(subject: Dict[str, Any], prop: Property) -> Dict[str, Any]:
    """Fill absent MLS facts without overwriting an agent's explicit subject correction."""
    merged = dict(subject or {})
    for field, value in {**_property_display_facts(prop), **_property_subject_facts(prop)}.items():
        if _missing_subject_fact(merged.get(field)) and not _missing_subject_fact(value):
            merged[field] = value
    return merged


def _subject_scoring_snapshot(subject: Dict[str, Any]) -> Dict[str, Any]:
    return {field: subject.get(field) for field in _AUTHORITATIVE_SUBJECT_FACT_FIELDS}


def _subject_scoring_fingerprint(subject: Dict[str, Any]) -> str:
    payload = json.dumps(_subject_scoring_snapshot(subject), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _canonical_subject_snapshot(db: AsyncSession, subject: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = dict(subject or {})
    listing_key = snapshot.get("listing_key")
    if snapshot.get("source", "mls") != "mls" or not listing_key:
        return snapshot
    prop = (await db.execute(select(Property).where(Property.listing_key == str(listing_key)))).scalar_one_or_none()
    from app.services import cma_time_machine
    _tm = cma_time_machine.current()
    if prop is not None and _tm and _tm.listing_key == str(listing_key):
        prop = cma_time_machine.masked_property(prop, _tm)
    return _merge_authoritative_subject_facts(snapshot, prop) if prop else snapshot


def _report_qa_blocking(report: CmaReport) -> list[str]:
    """Return the authoritative export blockers for the exact saved client artifact."""
    pricing = report.pricing or {}
    estimate = pricing.get("ai_estimate") or {}
    if not estimate:
        return []
    qa = estimate.get("qa_result") or {}
    blocking = list(estimate.get("qa_blocking") or []) + list(qa.get("blocking_errors") or [])
    if estimate.get("final_valuation") and not qa:
        blocking.append("The saved final valuation has not passed deterministic final-artifact QA.")
    if qa and qa.get("export_allowed") is False and not blocking:
        blocking.append("Deterministic final-artifact QA did not approve this report for export.")
    if not saved_artifact_matches(
        subject=report.subject or {}, comparables=report.comparables or [], pricing=pricing,
        market_context=report.market_context or {}, improvements=report.improvements or [],
    ):
        blocking.append("The saved client artifact changed after deterministic final QA.")
    return list(dict.fromkeys(str(item) for item in blocking if item))


# A report can be "blocked" for two very different reasons: (a) the saved fields drifted from
# what final QA last hashed - e.g. an agent filled in a required rating after generation, or a
# save round-tripped - with nothing about the AI's own valuation or prose actually needing to
# change, or (b) a real factual/reasoning problem the generation pass flagged in the comp notes
# or methodology text. Only (a) is free to fix: stamp_final_artifact_qa just re-hashes the
# CURRENT saved fields, no AI call. (b) needs recompute_valuation (a real, paid Claude +
# reasoning pass) since only that can rewrite the prose - see resync_qa below and its route.
_STALE_ARTIFACT_PATTERNS = (
    re.compile(r"changed after (deterministic )?final[- ]artifact qa", re.IGNORECASE),
    re.compile(r"changed after final qa", re.IGNORECASE),
    # The live hash-mismatch message from _report_qa_blocking itself ("The saved client artifact
    # changed after deterministic final QA.") - word order puts "deterministic" between "after"
    # and "final", which neither pattern above matches, and this is the single most common
    # staleness signal (fires on ANY post-generation save that doesn't recompute the hash).
    re.compile(r"client artifact changed after", re.IGNORECASE),
)


def _is_stale_artifact_only(blocking: list[str]) -> bool:
    return bool(blocking) and all(
        any(p.search(b) for p in _STALE_ARTIFACT_PATTERNS) for b in blocking
    )


async def resync_qa(db: AsyncSession, owner_id: str, report_id: str) -> CmaReport:
    """Free, deterministic re-stamp for a report whose ONLY problem is a stale artifact hash.
    No AI call, no cost, no reservation - just re-hashes the report's current saved fields and
    clears the staleness marker. Refuses (409) if anything else is also blocking; that needs a
    real recompute/regenerate, which costs money because it actually re-checks content."""
    report = await _get_owned(db, owner_id, report_id)
    blocking = _report_qa_blocking(report)
    if not blocking:
        return report
    if not _is_stale_artifact_only(blocking):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This report has a finding a free refresh can't clear - use Recompute or regenerate the analysis.",
        )
    pricing = dict(report.pricing or {})
    estimate = dict(pricing.get("ai_estimate") or {})
    if estimate:
        qa = dict(estimate.get("qa_result") or {})
        qa["blocking_errors"] = []
        qa["export_allowed"] = True
        qa["passed"] = True
        qa.pop("artifact_invalidated", None)
        estimate["qa_result"] = qa
        estimate["qa_blocking"] = []
        pricing["ai_estimate"] = estimate
    report.pricing = stamp_final_artifact_qa(
        subject=report.subject or {}, comparables=report.comparables or [], pricing=pricing,
        market_context=report.market_context or {}, improvements=report.improvements or [],
    )
    await db.commit()
    await db.refresh(report)
    return report


def _agent_display_name(user: User) -> str:
    return f"{user.first_name} {user.last_name}".strip()


async def _published_idx_site_url(db: AsyncSession, owner_id: str) -> Optional[str]:
    result = await db.execute(select(Site).where(Site.owner_id == owner_id, Site.status == "PUBLISHED", Site.is_enabled.is_(True)))
    site = result.scalar_one_or_none()
    if not site:
        return None

    domain_result = await db.execute(
        select(SiteDomain.hostname).where(
            SiteDomain.site_id == site.id,
            SiteDomain.status == "ACTIVE",
            SiteDomain.is_primary.is_(True),
        )
    )
    hostname = domain_result.scalar_one_or_none() or f"{site.subdomain_slug}.{OHP_BASE_DOMAIN}"
    from app.services.website.site_service import host_resolves
    if not await host_resolves(hostname):
        return None  # address doesn't open yet - the share link stays on openhousepal.com
    return f"https://{hostname}"


_GENERATION_STALE_AFTER_SECONDS = 1800  # a request still marked "generating" past this almost
# certainly crashed/restarted mid-run rather than genuinely still being in flight, so stop showing
# the client a spinner for it. Was 480s - shorter than a worst-case real run (main photo call +
# estimate-only retry + guidance pass + comp rewrites + the reasoning pass with up to 3 attempts at
# a 180s timeout each + the language polish), so a slow but healthy run could read as "failed".


def _reconcile_stale_generation(report: CmaReport) -> None:
    """A 'generating' status is only meaningful while the request that set it is still running.
    If the server died or restarted mid-generation, nothing would ever clear it - so treat one
    that's been sitting past a generous timeout as failed rather than leaving the client's
    generating animation spinning forever. Only mutates the in-memory object; callers that also
    commit for another reason will incidentally persist this, which is fine, but nothing here
    forces an extra write."""
    if report.generation_status != "generating" or not report.generation_started_at:
        return
    started = report.generation_started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if (datetime.now(timezone.utc) - started).total_seconds() > _GENERATION_STALE_AFTER_SECONDS:
        report.generation_status = "failed"


async def mark_generation_started(db: AsyncSession, owner_id: str, report_id: str) -> None:
    """Stamped right before the expensive part of generate_ai_analysis/recompute_valuation
    begins, and committed immediately (independent of whatever the caller does with `db` for the
    rest of the request) so a concurrent GET - the list page, or the same report reopened after
    navigating away - can see 'generating' right away instead of only after the whole run
    finishes."""
    await db.execute(
        update(CmaReport)
        .where(CmaReport.id == report_id, CmaReport.owner_id == owner_id)
        .values(generation_status="generating", generation_started_at=datetime.now(timezone.utc), generation_error=None)
    )
    await db.commit()


async def mark_generation_finished(owner_id: str, report_id: str, outcome: str, error: Optional[str] = None) -> None:
    """Called from the route handler's except/success paths to clear an in-flight marker. Uses
    its own short-lived session rather than the request's `db` - by the time this runs from an
    exception handler, that session may need a rollback before it can be used again, and this
    write must not depend on that. Only touches rows still marked 'generating' so it's a no-op
    for requests that never got that far (e.g. failed validation before any AI call).

    A run that COMPLETES may also overwrite 'failed': the only way this run's row reads 'failed'
    while the run itself just finished is the stale-timer guess (_reconcile_stale_generation)
    having been persisted by some unrelated commit mid-run - the finished report must not stay
    marked failed."""
    allowed = ("generating", "failed") if outcome == "complete" else ("generating",)
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(CmaReport)
            .where(CmaReport.id == report_id, CmaReport.owner_id == owner_id, CmaReport.generation_status.in_(allowed))
            .values(generation_status=outcome, generation_error=(error or None) if outcome != "complete" else None)
        )
        await session.commit()


async def list_reports(db: AsyncSession, owner_id: str) -> List[CmaReportListItem]:
    result = await db.execute(
        select(CmaReport)
        # Admin accuracy-test reports never appear in a report list (open them from the admin page).
        .where(CmaReport.owner_id == owner_id, CmaReport.is_backtest.is_(False))
        .order_by(CmaReport.updated_at.desc().nullslast(), CmaReport.created_at.desc())
    )
    reports = result.scalars().all()
    items = []
    for r in reports:
        _reconcile_stale_generation(r)
        subject = r.subject or {}
        pricing = r.pricing or {}
        ai_estimate = pricing.get('ai_estimate') or {}
        final_valuation = ai_estimate.get('final_valuation') or {}
        # Same priority as the range below (final_valuation/ai_estimate first, the authoritative
        # AI number) - the single point estimate the card actually leads with.
        fair_value = (
            final_valuation.get('value')
            if final_valuation.get('value') is not None
            else ai_estimate.get('value')
            if ai_estimate.get('value') is not None
            else pricing.get('estimated_fair_value')
            if r.mode == 'buyer'
            else pricing.get('most_likely_value')
        )
        if r.mode == 'buyer':
            # The CMA card is a valuation range, never an opening-offer range.
            value_range_min = final_valuation.get('range_min')
            value_range_max = final_valuation.get('range_max')
            if value_range_min is None:
                value_range_min = ai_estimate.get('range_min')
            if value_range_max is None:
                value_range_max = ai_estimate.get('range_max')
        else:
            # Same priority as the report detail page (suggested = final_valuation/ai_estimate
            # first): the AI range is the authoritative, freshest number. `recommended_min/max`
            # is an agent-editable field that only auto-fills once and then stays sticky, so after
            # a regenerate narrows the range it can go stale here while ai_estimate has already
            # moved on - reading it first kept the list card showing an old, wider range than the
            # report itself.
            value_range_min = (
                final_valuation.get('range_min')
                if final_valuation.get('range_min') is not None
                else ai_estimate.get('range_min')
                if ai_estimate.get('range_min') is not None
                else pricing.get('recommended_min')
                if pricing.get('recommended_min') is not None
                else pricing.get('comparable_suggested_min')
            )
            value_range_max = (
                final_valuation.get('range_max')
                if final_valuation.get('range_max') is not None
                else ai_estimate.get('range_max')
                if ai_estimate.get('range_max') is not None
                else pricing.get('recommended_max')
                if pricing.get('recommended_max') is not None
                else pricing.get('comparable_suggested_max')
            )
        address_parts = [subject.get('street_address'), subject.get('city'), subject.get('state')]
        address = ", ".join([p for p in address_parts if p]) or None
        qa_blocking = _report_qa_blocking(r)
        items.append(CmaReportListItem(
            id=r.id,
            client_name=r.client_name,
            mode=r.mode,
            status=r.status,
            share_slug=r.share_slug,
            subject_address=address,
            subject_photo_url=subject.get('photo_url'),
            comparable_count=len([c for c in (r.comparables or []) if c.get('is_included', True)]),
            fair_value=fair_value,
            value_range_min=value_range_min,
            value_range_max=value_range_max,
            default_theme=(r.presentation or {}).get('default_theme'),
            export_allowed=not qa_blocking,
            qa_blocking_count=len(qa_blocking),
            generation_status=r.generation_status,
            # The list card paces its own live % from this, and says why a run stopped.
            generation_started_at=r.generation_started_at,
            generation_error=r.generation_error if r.generation_status == 'failed' else None,
            updated_at=r.updated_at,
            created_at=r.created_at,
        ))
    return items


async def _get_owned(db: AsyncSession, owner_id: str, report_id: str) -> CmaReport:
    result = await db.execute(select(CmaReport).where(CmaReport.id == report_id, CmaReport.owner_id == owner_id))
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CMA not found")
    _reconcile_stale_generation(report)
    return report


async def create_report(db: AsyncSession, owner_id: str, payload: CmaReportCreate) -> CmaReport:
    subject = payload.subject.model_dump() if payload.subject else {}
    subject = await _canonical_subject_snapshot(db, subject)
    report = CmaReport(
        owner_id=owner_id,
        client_name=payload.client_name,
        mode=payload.mode,
        subject=subject,
    )
    db.add(report)
    await db.commit()
    await db.refresh(report)
    return report


async def get_report(db: AsyncSession, owner_id: str, report_id: str) -> CmaReport:
    report = await _get_owned(db, owner_id, report_id)
    # Drafts that predate richer subject capture are upgraded before the comp cards render.
    # Frozen/generated reports retain their exact saved evidence history.
    estimate = ((report.pricing or {}).get("ai_estimate") or {})
    if not estimate.get("final_valuation"):
        subject = await _canonical_subject_snapshot(db, report.subject or {})
        if subject != (report.subject or {}):
            report.subject = subject
            await db.commit()
            await db.refresh(report)
    return report


async def update_report(db: AsyncSession, owner_id: str, report_id: str, payload: CmaReportUpdate) -> CmaReport:
    report = await _get_owned(db, owner_id, report_id)
    data = payload.model_dump(exclude_unset=True)
    if "subject" in data:
        # CMA accuracy test only: a live arm for this agent + listing makes this an as-of test report.
        from app.services import cma_backtest_arms as _arms
        _arm = await _arms.find_arm(db, owner_id, listing_key=(data["subject"] or {}).get("listing_key"))
        with _arms.activated(_arm):
            data["subject"] = await _canonical_subject_snapshot(db, data["subject"] or {})
        if _arm:
            report.is_backtest = True
            _arm.report_id = report.id
    # Every export-readiness fix runs automatically inside the generation pipeline itself, before a
    # report is ever saved - there is no user-facing "this changed since final QA, go regenerate"
    # gate anymore (agents were hitting it on routine post-generation saves like filling in
    # mandatory comp ratings, which is normal workflow, not a real problem). A material edit after
    # marking a report READY/SHARED still reverts it to DRAFT so it's reviewed again, but saving
    # never blocks and never touches the AI's own qa_result.
    material_fields = ('subject', 'pricing', 'market_context', 'comparables', 'improvements')
    material_changed = any(field in data and data[field] != getattr(report, field) for field in material_fields)
    if material_changed and report.status in ('READY', 'SHARED') and 'status' not in data:
        data['status'] = 'DRAFT'
    for field in ('client_name', 'mode', 'status', 'subject', 'pricing', 'market_context', 'presentation', 'comparables', 'improvements', 'use_signature_card'):
        if field in data:
            setattr(report, field, data[field])
    await db.commit()
    await db.refresh(report)
    return report


async def update_default_theme(db: AsyncSession, owner_id: str, report_id: str, theme: str) -> None:
    """Merges just default_theme into the existing presentation dict, rather than a full
    presentation replace (what update_report above does) - the CMA list card only knows this one
    field, not the report's hidden_sections/hidden_comp_ids/cover_photo_url, so a full-object PATCH
    from there would silently wipe those back to defaults."""
    report = await _get_owned(db, owner_id, report_id)
    report.presentation = {**(report.presentation or {}), "default_theme": theme}
    await db.commit()


async def delete_report(db: AsyncSession, owner_id: str, report_id: str) -> None:
    report = await _get_owned(db, owner_id, report_id)
    await db.delete(report)
    await db.commit()


def _generate_slug(street: str = "") -> str:
    """The CMA's link: the home's street plus a random tail, e.g. /cma/847-lesley-rd-k3x9q2 - reads
    as the address, still unguessable (the report is private to whoever has the link)."""
    alphabet = string.ascii_lowercase + string.digits
    tail = ''.join(secrets.choice(alphabet) for _ in range(6 if street else 10))
    words = re.sub(r"[^a-z0-9]+", "-", (street or "").lower()).strip("-")[:48].strip("-")
    return f"{words}-{tail}" if words else tail


async def share_report(db: AsyncSession, owner_id: str, report_id: str) -> CmaShareResponse:
    report = await _get_owned(db, owner_id, report_id)
    if not report.share_slug:
        street = str((report.subject or {}).get("street_address") or (report.subject or {}).get("address") or "")
        for _ in range(10):
            candidate = _generate_slug(street)
            existing = await db.execute(select(CmaReport.id).where(CmaReport.share_slug == candidate))
            if existing.scalar_one_or_none() is None:
                report.share_slug = candidate
                break
    report.status = 'SHARED'
    await db.commit()
    await db.refresh(report)
    return CmaShareResponse(share_slug=report.share_slug, status=report.status, public_path=f"/cma/{report.share_slug}")


async def get_public_report(db: AsyncSession, slug: str) -> CmaPublicResponse:
    result = await db.execute(select(CmaReport).where(CmaReport.share_slug == slug))
    report = result.scalar_one_or_none()
    # (Accuracy-test reports can be shared like any other so the test can open the client page the
    # way an agent would; they live on the test agent's account and are never listed.)
    if not report or report.status != 'SHARED':
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This CMA link isn't active.")

    owner_result = await db.execute(select(User).where(User.id == report.owner_id))
    owner = owner_result.scalar_one_or_none()
    # Same rule as Showcases, blogs and forms: a client link only works while the agent's plan
    # includes it (it comes straight back if they resubscribe).
    from app.utils.auth import owner_has_access, MLS_ACCESS_TIERS
    if not owner or not owner_has_access(owner, MLS_ACCESS_TIERS):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This CMA link isn't active.")
    brand_result = await db.execute(select(StudioBrandKit).where(StudioBrandKit.owner_id == report.owner_id))
    brand = brand_result.scalar_one_or_none()
    hub_result = await db.execute(select(AgentHub).where(AgentHub.owner_id == report.owner_id))
    hub = hub_result.scalar_one_or_none()
    idx_site_url = await _published_idx_site_url(db, report.owner_id)
    hub_config = hub.config if hub and isinstance(hub.config, dict) else {}
    headshot_url = hub_config.get("profilePhotoUrl") or hub_config.get("profile_photo_url") or (owner.photo_url if owner else None)

    agent = CmaPublicAgent(
        name=_agent_display_name(owner) if owner else "Your Agent",
        headshot_url=headshot_url,
        brokerage=owner.brokerage if owner else None,
        phone=((hub_config.get("contact") or {}).get("call") or None),
        email=(((hub_config.get("contact") or {}).get("email")) or (owner.email if owner else None)),
        logo_url=((brand.personal_logo_url or brand.brokerage_logo_url) if brand else None),
        primary_color=brand.primary_color if brand else None,
        secondary_color=brand.secondary_color if brand else None,
        agent_hub_slug=hub.slug if hub else None,
        idx_site_url=idx_site_url,
    )
    from app.services import email_signature_service
    signature_hub_slug = hub.slug if (hub and hub.slug and report.use_signature_card is not False and await email_signature_service.has_signature_card(db, owner)) else None

    return CmaPublicResponse(
        client_name=report.client_name,
        mode=report.mode,
        subject=report.subject or {},
        comparables=report.comparables or [],
        pricing=report.pricing or {},
        market_context=report.market_context or {},
        improvements=report.improvements or [],
        presentation=report.presentation or {},
        agent=agent,
        updated_at=report.updated_at,
        signature_hub_slug=signature_hub_slug,
    )


def _clean_status(home_status: Optional[str]) -> str:
    return (home_status or "").upper().replace("-BRIGHT", "")


def _categorize_status(home_status: Optional[str]) -> Optional[str]:
    s = _clean_status(home_status)
    if s.startswith("CLOSED") or s.startswith("SOLD"):
        return "sold"
    if s.startswith("ACTIVE") and "CONTRACT" in s:
        return "active_under_contract"
    if s.startswith("PENDING") or "CONTRACT" in s:
        return "pending"
    if s.startswith("ACTIVE") or s.startswith("COMING SOON"):
        return "active"
    return None


def _parse_date(value) -> Optional[date]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).date()
    except ValueError:
        try:
            return datetime.strptime(value[:10], '%Y-%m-%d').date()
        except ValueError:
            return None


def _score_comparable_candidates(
    subject: Dict[str, Any],
    candidates: List[CmaCompCandidate],
    similarity_context: Dict[str, Any],
) -> List[CmaCompCandidate]:
    """Overwrite any incoming/stale badge with the backend's authoritative score and trace."""
    # Import locally because the generation service consumes cma_service too. This keeps one
    # authoritative implementation without creating an import cycle at module startup.
    from app.services.cma_ai_service import _continuous_relevance

    relevance = _continuous_relevance(
        subject,
        [candidate.model_dump() for candidate in candidates],
        density_context=similarity_context,
    )
    rows_by_key = {row["listing_key"]: row for row in relevance["comps"]}
    scored_candidates: List[CmaCompCandidate] = []
    for candidate in candidates:
        row = rows_by_key[candidate.listing_key]
        raw_relevance = row["adjusted_relevance_score"]
        match_score = calibrate_match_percentage(raw_relevance)
        details = {
            "version": relevance.get("version"),
            "display_calibration_version": MATCH_DISPLAY_CALIBRATION_VERSION,
            "density_context": similarity_context,
            "subject_snapshot_sha256": similarity_context["subject_snapshot_sha256"],
            "candidate_distance_miles": row.get("candidate_distance_miles"),
            "component_scores": row.get("components"),
            "component_points": row.get("component_points"),
            "available_component_weight": row.get("available_component_weight"),
            "proximity_score_points": row.get("proximity_score_points"),
            "proximity_max_points": row.get("proximity_max_points"),
            "base_relevance_score": row.get("base_relevance_score"),
            "raw_relevance_score": raw_relevance,
            "display_match_score": match_score,
            "final_similarity_score": match_score,
            "scoring_authority": "backend_continuous_relevance",
            # Eligibility only - never changes match_score above. A structurally incompatible
            # property type (condo vs single-family, townhouse vs detached single-family, etc.)
            # cannot become a Strong/primary comp; see _continuous_relevance's role gate.
            "proposed_role": row.get("proposed_role"),
            "property_type_incompatible": row.get("structurally_incompatible_property_type", False),
            # Can this record carry ANY valuation weight (verified closing price for a sale, and a
            # finished area on record)? The wizard never auto-picks one that can't - it would only
            # take one of the five slots from a usable sale.
            "valuation_eligible": bool(row.get("mechanically_eligible")),
        }
        scored_candidates.append(candidate.model_copy(update={
            "match_score": match_score,
            "similarity_details": details,
        }))
    return scored_candidates


def is_lease_listing(prop_or_subject) -> bool:
    """A rental listing (Bright 'Residential Lease', or a monthly-rent-sized price). A CMA values a
    sale - 1420 Locust St #23H ($1,995/month, 10/21) was valued at "$1,850" against lease comps."""
    get = (lambda k: prop_or_subject.get(k)) if isinstance(prop_or_subject, dict) else (lambda k: getattr(prop_or_subject, k, None))
    raw = get("raw_mls_data") or {}
    kinds = " ".join(str(v or "") for v in (get("home_type"), get("property_type"), get("mls_property_type"),
                                            raw.get("PropertyType") if isinstance(raw, dict) else None)).lower()
    if "lease" in kinds.replace("_", " "):
        return True
    price = get("list_price") or get("price")
    try:
        return bool(price) and 0 < float(price) < MIN_SALE_PRICE
    except (TypeError, ValueError):
        return False


# A sale under this, or a sale-to-list ratio outside these bounds, is a data error or a lease - never a
# comp (814 Hessian Ave: "$500 list -> $197,500 sold, 39,400% over asking", grader 10/21).
MIN_SALE_PRICE = 10_000
SALE_TO_LIST_BOUNDS = (0.5, 2.0)

# Same product only (grader 10/21): a condo's comps are condos, a townhouse's are townhouses/twins.
# Families as in cma_ai_service._type_family; FARM counts as detached.
_FAMILY_HOME_TYPES = {
    "detached": (HomeType.SINGLE_FAMILY.value, HomeType.FARM.value),
    "attached": (HomeType.TOWNHOUSE.value,),
    "condo": (HomeType.CONDO.value,),
    "multi": (HomeType.MULTI_FAMILY.value,),
}


def _comp_sanity_problem(c: "CmaCompCandidate") -> Optional[str]:
    if not (c.street_address or "").strip() or len((c.street_address or "").strip()) < 4:
        return "no street address"
    if c.status == "sold" and c.sale_price:
        if c.sale_price < MIN_SALE_PRICE:
            return "sale price under $10,000"
        if c.list_price and c.list_price > 0:
            ratio = c.sale_price / c.list_price
            if not (SALE_TO_LIST_BOUNDS[0] <= ratio <= SALE_TO_LIST_BOUNDS[1]):
                return "sale-to-list ratio out of range"
    if (c.beds or 0) >= 1 and c.baths is not None and c.baths <= 0:
        return "bedrooms with no bathrooms on record"
    return None


async def search_comparables(db: AsyncSession, req: CmaCompSearchRequest, *, keep_all: bool = False,
                             row_limit: int = 400) -> List[CmaCompCandidate]:
    """Finds candidate comparables around the subject from the local MLS mirror. This suggests -
    it never decides - which properties belong in the CMA; the agent picks from these results.
    `keep_all` returns every scored candidate (auto_search_comparables walks its widening steps on
    them) instead of the best 60 for the grid."""
    if req.latitude is None or req.longitude is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Subject property location is required to search for comparables.")

    lat_offset, lng_offset = get_lat_long_offsets(req.latitude, req.radius_miles)
    query = select(Property).where(
        Property.latitude.between(req.latitude - lat_offset, req.latitude + lat_offset),
        Property.longitude.between(req.longitude - lng_offset, req.longitude + lng_offset),
        # Agent-entered "My Listings" rows are not real MLS records - never offer them as CMA
        # comparable candidates.
        Property.is_manual.is_(False),
        # A rental's completed lease also lands on MlsStatus=CLOSED-BRIGHT, same as an actual
        # sale - home_type is what actually distinguishes them, so a CMA (always sale comps)
        # must exclude leases explicitly rather than relying on status alone. NULL-safe (keeps
        # rows with no home_type at all) so this can only ever exclude a row we're confident is
        # actually a lease, never silently drop an unmapped sold comp.
        or_(Property.home_type.is_(None), Property.home_type != HomeType.RESIDENTIAL_LEASE.value),
        # Vacant land and commercial records are never a comparable sale for a home (a land/lot
        # sale has no living area; a commercial sale prices a business). NULL-safe like the lease rule.
        or_(Property.home_type.is_(None), Property.home_type.notin_([HomeType.LAND.value, HomeType.COMMERCIAL.value])),
        # Bright files some rentals as 'Multi-Family' with a monthly rent as the "close price" - the
        # lease is only in PropertyType (1420 Locust St #23H got two of them as comps, 10/21).
        ~func.coalesce(Property.raw_mls_data["PropertyType"].astext, "").ilike("%lease%"),
    )
    # Same state only - no New Jersey comps for a Philadelphia home (grader 10/21).
    _subj_state = ((req.subject.state if req.subject else None) or "").strip().upper()
    if _subj_state:
        query = query.where(or_(Property.state.is_(None), func.upper(Property.state) == _subj_state))
    # Same product only: a condo's comps are condos, a townhouse's townhouses (grader 10/21). Rows with
    # no type on record stay in and are scored.
    from app.services.cma_ai_service import _type_family as _tf
    _family = _tf(req.property_type or (req.subject.property_type if req.subject else None))
    if _family in _FAMILY_HOME_TYPES:
        query = query.where(or_(Property.home_type.is_(None), Property.home_type == HomeType.OTHER.value,
                                Property.home_type.in_(_FAMILY_HOME_TYPES[_family])))
    if req.listing_key:
        query = query.where(Property.listing_key != req.listing_key)
    # The subject's OWN earlier sales are separate MLS listings (different listing_key) of the same
    # house - they used to come back as a "98% match" comp and anchor the value to itself. Same
    # street address + ZIP, or the same tax parcel, is never a comparable.
    subj = req.subject
    subj_street = " ".join((subj.street_address or "").upper().replace(".", "").split()) if subj else ""
    if subj_street:
        same_street = func.upper(func.trim(Property.street_address)) == subj_street
        if subj.zipcode:
            query = query.where(~and_(same_street, func.left(func.coalesce(Property.zipcode, ""), 5) == str(subj.zipcode)[:5]))
        elif subj.city:
            query = query.where(~and_(same_street, func.upper(func.coalesce(Property.city, "")) == subj.city.upper()))
    if req.listing_key:
        tax_id = (await db.execute(select(Property.listing_tax_id).where(Property.listing_key == req.listing_key))).scalar_one_or_none()
        if tax_id and len(tax_id.strip()) >= 6 and any(ch.isdigit() for ch in tax_id):
            query = query.where(or_(Property.listing_tax_id.is_(None), Property.listing_tax_id != tax_id))
    if req.exclude_listing_keys:
        query = query.where(Property.listing_key.notin_(req.exclude_listing_keys))
    if req.min_price is not None:
        query = query.where(Property.price >= req.min_price)
    if req.max_price is not None:
        query = query.where(Property.price <= req.max_price)
    if req.min_sqft is not None:
        query = query.where(Property.living_area >= req.min_sqft)
    if req.max_sqft is not None:
        query = query.where(Property.living_area <= req.max_sqft)
    if req.min_lot_size is not None:
        query = query.where(Property.lot_size >= req.min_lot_size)
    if req.max_lot_size is not None:
        query = query.where(Property.lot_size <= req.max_lot_size)
    if req.min_beds is not None:
        query = query.where(Property.bedrooms >= req.min_beds)
    if req.min_baths is not None:
        query = query.where(Property.bathrooms >= req.min_baths)
    if req.min_year_built is not None:
        query = query.where(Property.year_built >= req.min_year_built)
    if req.max_year_built is not None:
        query = query.where(Property.year_built <= req.max_year_built)
    if req.school_district:
        query = query.where(Property.school_district_name.ilike(req.school_district))
    if req.city:
        query = query.where(Property.city.ilike(req.city))

    # The circular radius filter AND the nearest-first ordering both run in SQL, so the LIMIT below
    # caps the comps that are ACTUALLY inside the circle - not an arbitrary slice of a dense
    # bounding box taken before the circle was ever checked. That old ordering was a squared-degree
    # proxy + LIMIT 300: in a dense market like the Main Line it routinely dropped genuinely
    # in-radius comps, and widening the radius surfaced nothing new because the 300 slots were
    # already full of closer rows. The bounding box stays as a cheap, index-friendly pre-filter
    # ahead of the (non-sargable) haversine. LIMIT is generous because the Python loop below still
    # drops rows on status / sale-date / lease before the final trim.
    distance_expr = distance_miles_sql(Property.latitude, Property.longitude, req.latitude, req.longitude)
    query = query.where(distance_expr <= req.radius_miles)

    date_from = _parse_date(req.sale_date_from)
    date_to = _parse_date(req.sale_date_to)
    # Status + sale-date windows in SQL too, BEFORE the nearest-first LIMIT (the Python loop below
    # still makes the exact call). Otherwise the 400 nearest rows are mostly old sales / actives /
    # other statuses and a wider radius never reaches the in-window sales it was widened for.
    status_u = func.upper(func.coalesce(Property.home_status, ""))
    is_closed = or_(status_u.like("CLOSED%"), status_u.like("SOLD%"))
    status_terms = []
    if "sold" in req.statuses:
        status_terms.append(is_closed)
    if "active" in req.statuses or "active_under_contract" in req.statuses:
        status_terms += [status_u.like("ACTIVE%"), status_u.like("COMING SOON%")]
    if "pending" in req.statuses:
        status_terms += [status_u.like("PENDING%"), status_u.like("%CONTRACT%")]
    if status_terms:
        query = query.where(or_(*status_terms))
    if date_from or date_to:
        sale_day = func.coalesce(
            Property.raw_mls_data["CloseDate"].astext,
            func.to_char(Property.updated_at, "YYYY-MM-DD"),
        )
        date_terms = []
        if date_from:
            date_terms.append(sale_day >= date_from.isoformat())
        if date_to:
            date_terms.append(func.substr(sale_day, 1, 10) <= date_to.isoformat())
        query = query.where(or_(~is_closed, and_(*date_terms)))
    result = await db.execute(query.order_by(distance_expr.asc()).limit(row_limit))
    rows = result.scalars().all()

    candidates: List[CmaCompCandidate] = []
    for p in rows:
        # SQL already enforced the circle; this is a float-parity safety net (Postgres vs. Python
        # haversine) and guards a row whose coords went NULL between query and read.
        if not is_within_distance(req.latitude, req.longitude, p.latitude, p.longitude, req.radius_miles):
            continue
        category = _categorize_status(p.home_status)
        if category not in req.statuses:
            continue

        raw = p.raw_mls_data or {}

        # ---- Verified closed-sale price (never a list / pending / current-price fallback) ----
        # A comp counts as a CLOSED valuation comp only if the mirror says it's closed AND a real
        # ClosePrice is present. Anything else -> price_verified=False, no sale_price, no $/sqft,
        # and the AI + all aggregates treat it as market-context-only.
        raw_close = raw.get('ClosePrice')
        try:
            close_price_val = float(raw_close) if raw_close not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            close_price_val = None
        price_verified = bool(category == 'sold' and close_price_val and close_price_val > 0)
        sale_price = close_price_val if price_verified else None

        close_date_raw = raw.get('CloseDate')
        sale_date = None
        if category == 'sold':
            parsed = _parse_date(close_date_raw) or (p.updated_at.date() if p.updated_at else None)
            # A closing date in the future is a data error (seen: 2026-12-31 in the live sold set) -
            # never offer it as a comparable sale.
            if _parse_date(close_date_raw) and _parse_date(close_date_raw) > date.today():
                continue
            if date_from and (not parsed or parsed < date_from):
                continue
            if date_to and (not parsed or parsed > date_to):
                continue
            # Only trust a real MLS CloseDate as the sale date; the sync-timestamp fallback is a
            # rough proxy for the date filter above, not a fact to show as "sold on".
            sale_date = close_date_raw if _parse_date(close_date_raw) else None

        list_price = p.price
        original_list_price = None
        _olp = raw.get('OriginalListPrice')
        try:
            original_list_price = float(_olp) if _olp not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            original_list_price = None

        # $/sqft is computed ONLY from a verified closed price (sold comps) or the current list
        # price (active/pending comps, where the asking price is the relevant number). A sold comp
        # with an unverified price gets no $/sqft at all.
        sqft = p.living_area
        if category == 'sold':
            psf_basis = sale_price if price_verified else None
        else:
            psf_basis = list_price
        price_per_sqft = round(psf_basis / sqft, 2) if psf_basis and sqft else None

        # DOM straight from MLS when present; active-only fallback uses the shared policy.
        dom_raw = raw.get('DaysOnMarket')
        cdom_raw = raw.get('CumulativeDaysOnMarket') or raw.get('CDOMBright') or raw.get('CDOM')
        try:
            dom_val = resolve_days_on_market(
                dom_raw if dom_raw not in (None, "") else p.days_on_market,
                p.home_status,
                p.mls_list_date,
                # Bright's DOM is the count on the day we read it - still-active comps keep counting.
                fetched_at=dom_fetched_at(p),
            )
        except (TypeError, ValueError):
            dom_val = None
        try:
            cdom_val = int(cdom_raw) if cdom_raw not in (None, "") else None
        except (TypeError, ValueError):
            cdom_val = None
        distance = haversine_distance(req.latitude, req.longitude, p.latitude, p.longitude) if p.latitude and p.longitude else None

        garage = None
        if p.has_garage and p.garage_spaces:
            garage = f"{int(p.garage_spaces)} Car"
        elif p.has_garage:
            garage = "Yes"
        elif p.has_garage is False:
            garage = "None"

        basement = None
        raw_basement = raw.get('Basement')
        if isinstance(raw_basement, list) and raw_basement:
            basement = ", ".join(raw_basement)
        elif raw.get('BasementYN') is True:
            basement = "Yes"
        elif raw.get('BasementYN') is False:
            basement = "None"
        if basement is None:
            # Basement/BasementYN weren't populated in the raw MLS payload for this listing -
            # before giving up, check the parsed below_grade_finished_area/has_basement columns,
            # since Bright sometimes only reports basement presence through square footage, not
            # the Basement/BasementYN fields. Always resolves to an explicit "None" rather than
            # silently leaving the stat blank, so the frontend can show "no basement" instead of
            # hiding the field entirely.
            if p.below_grade_finished_area:
                basement = "Finished"
            elif p.has_basement is True:
                basement = "Yes"
            else:
                basement = "None"

        candidates.append(CmaCompCandidate(
            listing_key=p.listing_key,
            status=category,
            photo_url=p.img_src,
            photos=[url for url in (p.photos or []) if isinstance(url, str)],
            street_address=p.street_address,
            city=p.city,
            state=p.state,
            zipcode=p.zipcode,
            latitude=p.latitude,
            longitude=p.longitude,
            distance_miles=round(distance, 2) if distance is not None else None,
            sale_price=sale_price,
            list_price=list_price,
            original_list_price=original_list_price,
            close_price=sale_price,
            close_date=sale_date if price_verified else None,
            sale_date=sale_date,
            price_verified=price_verified,
            days_on_market=dom_val,
            cdom=cdom_val,
            sqft=sqft,
            above_grade_finished_area=p.above_grade_finished_area,
            below_grade_finished_area=p.below_grade_finished_area,
            lot_size=p.lot_size,
            beds=p.bedrooms,
            baths=_mls_baths(p),
            year_built=p.year_built,
            architectural_style=p.architectural_style,
            levels=p.levels,
            township=p.township,
            subdivision_name=p.subdivision_name,
            garage=garage,
            basement=basement,
            price_per_sqft=price_per_sqft,
            school_district=raw.get('SchoolDistrictName') or p.school_district_name,
            water_source=raw.get('WaterSource') or p.water_source,
            sewer=raw.get('Sewer') or p.sewer,
            public_remarks=raw.get('PublicRemarks'),
            annual_taxes=raw.get('TaxAnnualAmount'),
            property_type=p.home_type,
        ))

    # Records that can't be a real sale never reach a report (no street address, a $500 list price,
    # 4 bedrooms and 0 baths - grader 10/21).
    candidates = [c for c in candidates if not _comp_sanity_problem(c)]
    candidates.sort(key=lambda c: (c.distance_miles is None, c.distance_miles))
    # One house, one comp: a home that sold more than once in the window (or has a sale plus a
    # current listing) kept every record, so the same house could be picked twice and counted
    # double. Keep its most relevant record - its latest closed sale.
    def _home_key(c):
        return (" ".join((c.street_address or "").upper().split()), (c.zipcode or c.city or "").upper()[:5])
    def _rank(c):
        return (c.status == 'sold', str(c.sale_date or ""))
    best: dict = {}
    for c in candidates:
        k = _home_key(c)
        if not k[0]:
            best[id(c)] = c
            continue
        if k not in best or _rank(c) > _rank(best[k]):
            best[k] = c
    keep = {id(v) for v in best.values()}
    candidates = [c for c in candidates if id(c) in keep]
    # One filter-independent density context for the subject/search. Every candidate receives the
    # same frozen context, so a candidate cannot improve its own score by changing the scale.
    requested_subject = req.subject.model_dump() if req.subject else {
        "listing_key": req.listing_key,
        "source": "mls",
        "latitude": req.latitude,
        "longitude": req.longitude,
        "sqft": req.subject_sqft or ((req.min_sqft + req.max_sqft) / 2 if req.min_sqft and req.max_sqft else None),
        "property_type": req.property_type,
    }
    if not requested_subject.get("listing_key") and req.listing_key:
        requested_subject["listing_key"] = req.listing_key
    canonical_subject = await _canonical_subject_snapshot(db, requested_subject)
    similarity_context = await load_local_proximity_context(db, canonical_subject)
    similarity_context = {
        **similarity_context,
        "subject_snapshot": _subject_scoring_snapshot(canonical_subject),
        "subject_snapshot_sha256": _subject_scoring_fingerprint(canonical_subject),
    }
    # Score EVERY in-circle candidate, then keep the best 60 for the grid: same housing product
    # first (a twin's comps are twins), then usable sales (a verified price the value math can use),
    # then Match %. It used to keep the 60 NEAREST before scoring, so in a dense area widening the
    # radius changed nothing and a strong verified sale 2 miles out never appeared (417 Dogwood Ln:
    # 2702 E Kings Hwy, verified, 80%, cut by 60 nearer rows - 10/20).
    from app.services.cma_ai_service import _type_family
    _subject_family = _type_family(req.property_type or (req.subject.property_type if req.subject else None))
    scored = _score_comparable_candidates(canonical_subject, [
        candidate.model_copy(update={"similarity_context": similarity_context}) for candidate in candidates
    ], similarity_context)

    def _grid_rank(c: CmaCompCandidate):
        d = c.similarity_details or {}
        other_product = bool(_subject_family) and _type_family(c.property_type) not in (None, _subject_family)
        return (other_product, d.get("valuation_eligible") is False, -(c.match_score or 0), c.distance_miles or 0)

    ranked = sorted(scored, key=_grid_rank)
    return ranked if keep_all else ranked[:60]


# ---- Automatic comp search (grader 10/21) ----------------------------------------------------------
# It used to widen the radius 1 -> 2 -> ... -> 25 mi until FIVE comps scored 70%+, which a one-of-a-kind
# home never reaches (239 Windham Dr topped out at 67%), so nearly every search went to 25 mi. Now:
# time first (6 -> 12 -> 18 months), then distance, with a radius ladder by product and density, and it
# stops when there are enough USABLE comps. Short of that at the limit, it says so instead of widening.
AUTO_SOLD_DAYS = (183, 365, 548)
AUTO_RADII = {
    "urban": (0.25, 0.5, 1.0),          # condos, and city rowhomes/twins: same building first
    "attached": (0.5, 1.0, 2.0, 3.0),   # suburban townhouses/twins
    "detached": (1.0, 2.0, 3.0, 5.0),
}
AUTO_RURAL_EXTRA_RADII = (8.0, 10.0)    # detached only, when 5 mi still isn't enough
AUTO_USABLE_MIN_SCORE = 55
AUTO_ENOUGH_USABLE = 5
AUTO_LIMITED_BELOW = 3                  # fewer usable comps than this -> "Limited comparable sales"
# A city: this many same-product sales within half a mile in the last year
_URBAN_DENSITY_SALES = 25
# Price band around the asking price for the automatic search (fraction each way). Backtest knob.
_AUTO_PRICE_BAND = float(os.environ.get("CMA_AUTO_PRICE_BAND", "0.35"))


def _auto_pickable(c: CmaCompCandidate) -> bool:
    d = c.similarity_details or {}
    return not d.get("property_type_incompatible") and d.get("valuation_eligible") is not False


def _sale_age_days(c: CmaCompCandidate, today: date) -> Optional[int]:
    d = _parse_date(c.sale_date or c.close_date)
    return (today - d).days if d else None


# A usable comp must also be of the subject's era: within this many years of its build year (0 = any).
# 239 Windham Dr (2012) V2: five 1990-2000 sales at 1.8 mi counted as "enough", so the search never
# reached the 2010s homes 3-5 mi out (grader, 10/21). Backtest knob.
AUTO_USABLE_MAX_YEAR_GAP = int(os.environ.get("CMA_AUTO_AGE_WINDOW", "0"))


# Auto-pick: sales from the subject's era first when there are at least 3 of them (backtest knob, 0 = off)
AUTO_PICK_ERA_YEARS = int(os.environ.get("CMA_AUTO_PICK_ERA", "0"))


def era_first(ranked: list, subject_year) -> list:
    """Keep the Match order, but move sales built within AUTO_PICK_ERA_YEARS of the subject ahead of the rest
    when there are at least 3 of them - a 2012 home's comps are 2000s-2010s homes, not 1990s ones."""
    if not (AUTO_PICK_ERA_YEARS and subject_year):
        return ranked
    same = [c for c in ranked if c.year_built and abs(float(c.year_built) - float(subject_year)) <= AUTO_PICK_ERA_YEARS]
    if len(same) < 3:
        return ranked
    return same + [c for c in ranked if c not in same]


def plan_auto_search(candidates: List[CmaCompCandidate], family: Optional[str], *, today: date,
                     subject_year: Optional[float] = None) -> dict:
    """Walk the widening steps over one scored candidate pool (fetched at the widest window). Pure, so
    the backtest and the golden tests run exactly what agents get. Returns the chosen radius / sold
    window, whether comps are limited, and the steps taken."""
    near_year = sum(1 for c in candidates if (c.distance_miles or 99) <= 0.5 and (_sale_age_days(c, today) or 9999) <= 365)
    if family == "condo" or (family == "attached" and near_year >= _URBAN_DENSITY_SALES):
        ladder, extra = AUTO_RADII["urban"], ()
    elif family == "attached":
        ladder, extra = AUTO_RADII["attached"], ()
    else:
        ladder, extra = AUTO_RADII["detached"], AUTO_RURAL_EXTRA_RADII

    def era_ok(c) -> bool:
        if not (AUTO_USABLE_MAX_YEAR_GAP and subject_year and c.year_built):
            return True
        return abs(float(c.year_built) - float(subject_year)) <= AUTO_USABLE_MAX_YEAR_GAP

    def usable(radius: float, days: int) -> int:
        return sum(1 for c in candidates
                   if (c.distance_miles or 0) <= radius and (_sale_age_days(c, today) or 0) <= days
                   and _auto_pickable(c) and (c.match_score or 0) >= AUTO_USABLE_MIN_SCORE and era_ok(c))

    steps, chosen = [], None
    for ri, radius in enumerate(ladder):
        for days in AUTO_SOLD_DAYS:
            n = usable(radius, days)
            steps.append({"radius_mi": radius, "sold_within_days": days, "usable": n})
            if n >= AUTO_ENOUGH_USABLE:
                chosen = (ri, days)
                break
        if chosen:
            break
    if chosen:
        ri, days = chosen
        # One radius step past "enough": every candidate is scored (distance is part of the score), so a
        # wider circle only adds choices - 417 Dogwood Ln's best sale sat just past the first circle.
        radius = ladder[min(ri + 1, len(ladder) - 1)]
    else:
        radius, days = ladder[-1], AUTO_SOLD_DAYS[-1]
        for r in extra:   # rural: a little farther, never 25 mi
            n = usable(r, days)
            steps.append({"radius_mi": r, "sold_within_days": days, "usable": n})
            radius = r
            if n >= AUTO_ENOUGH_USABLE:
                break
    final_usable = usable(radius, days)
    return {"radius_miles": radius, "sold_within_days": days, "usable": final_usable,
            "limited": final_usable < AUTO_LIMITED_BELOW, "steps": steps,
            "density": "urban" if ladder is AUTO_RADII["urban"] else ("suburban" if not extra else "suburban/rural")}


# ---- Current competition (grader, 10/21) -----------------------------------------------------------
# Similar homes for sale or under contract right now - shown next to the sold comps, never in the grid
# or the sold charts. Same matching rules as the sold search (type, state, no rentals, same building
# first for condos); actives are context only, pendings may nudge the value review a little.
COMPETITION_RADIUS = {"condo": 1.0, "attached": 2.0, "detached": 3.0}
COMPETITION_MIN_SCORE = 50
COMPETITION_MAX_PER_STATUS = 3
COMPETITION_MAX = 5


async def _price_cuts(db: AsyncSession, listing_keys: List[str]) -> Dict[str, dict]:
    from app.models.database import PropertyPriceEvent
    if not listing_keys:
        return {}
    rows = (await db.execute(
        select(PropertyPriceEvent.listing_key, PropertyPriceEvent.price, PropertyPriceEvent.previous_price, PropertyPriceEvent.observed_at)
        .where(PropertyPriceEvent.listing_key.in_(listing_keys), PropertyPriceEvent.kind == "price")
    )).all()
    out: Dict[str, dict] = {}
    for key, price, prev, at in rows:
        if price and prev and price < prev:
            d = out.setdefault(key, {"count": 0, "total": 0.0, "first_price": prev, "last_cut_at": None})
            d["count"] += 1
            d["total"] += prev - price
            d["first_price"] = max(d["first_price"], prev)
            d["last_cut_at"] = max(filter(None, [d["last_cut_at"], at.date().isoformat() if at else None]), default=None)
    return out


async def current_competition(db: AsyncSession, subject: dict, *, listing_key: Optional[str] = None,
                              today: Optional[date] = None) -> List[dict]:
    """Up to 5 similar ACTIVE and PENDING / under-contract homes (at most 3 of each), best Match first.
    List prices, not closed sales."""
    from app.services.cma_ai_service import _type_family as _tf
    from app.schemas.cma import CmaSubjectProperty
    family = _tf(subject.get("property_type"))
    lp = subject.get("list_price")
    fields = set(CmaSubjectProperty.model_fields)
    req = CmaCompSearchRequest(
        listing_key=listing_key or subject.get("listing_key"),
        subject=CmaSubjectProperty(**{k: v for k, v in subject.items() if k in fields and v is not None}),
        latitude=subject.get("latitude"), longitude=subject.get("longitude"),
        subject_sqft=subject.get("above_grade_finished_area") or subject.get("sqft"),
        property_type=subject.get("property_type"),
        radius_miles=COMPETITION_RADIUS.get(family, COMPETITION_RADIUS["detached"]),
        statuses=["active", "active_under_contract", "pending"],
        min_price=round(lp * (1 - _AUTO_PRICE_BAND)) if lp else None,
        max_price=round(lp * (1 + _AUTO_PRICE_BAND)) if lp else None,
    )
    if req.latitude is None or req.longitude is None:
        return []
    pool = await search_comparables(db, req, keep_all=True, row_limit=600)
    pool = [c for c in pool if _auto_pickable(c) and (c.match_score or 0) >= COMPETITION_MIN_SCORE and c.list_price]
    pool.sort(key=lambda c: -(c.match_score or 0))
    pend = [c for c in pool if c.status in ("pending", "active_under_contract")][:COMPETITION_MAX_PER_STATUS]
    act = [c for c in pool if c.status == "active"][:COMPETITION_MAX_PER_STATUS]
    picked = sorted(pend + act, key=lambda c: -(c.match_score or 0))[:COMPETITION_MAX]
    cuts = await _price_cuts(db, [c.listing_key for c in picked])
    # Builder listings (Bright NewConstructionYN): shown, labelled, never a guide to a resale's value
    # (510 Newcomen Rd #B3D - a to-be-built floor plan - moved 239 Windham Dr, V2 10/21).
    _keys = [c.listing_key for c in picked]
    _new_rows = (await db.execute(select(Property.listing_key, Property.raw_mls_data["NewConstructionYN"].astext)
                                   .where(Property.listing_key.in_(_keys)))).all() if _keys else []
    new_build = {k for k, v in _new_rows if str(v).lower() in ("true", "1", "yes")}
    out = []
    for c in picked:
        ag = c.above_grade_finished_area or c.sqft
        out.append({
            "listing_key": c.listing_key, "status": "pending" if c.status in ("pending", "active_under_contract") else "active",
            "street_address": c.street_address, "city": c.city, "state": c.state, "zipcode": c.zipcode,
            "photo_url": c.photo_url, "list_price": c.list_price,
            "price_per_sqft": round(c.list_price / ag) if c.list_price and ag else None,
            "beds": c.beds, "baths": c.baths, "sqft": ag, "year_built": c.year_built,
            "days_on_market": c.days_on_market, "distance_miles": c.distance_miles, "match_score": c.match_score,
            "latitude": c.latitude, "longitude": c.longitude,
            "price_cuts": cuts.get(c.listing_key),
            "new_construction": c.listing_key in new_build,
            "lot_size": c.lot_size, "below_grade_finished_area": c.below_grade_finished_area,
            "garage": c.garage, "basement": c.basement, "water_source": c.water_source, "sewer": c.sewer,
            "school_district": c.school_district, "township": c.township, "subdivision_name": c.subdivision_name,
            "property_type": c.property_type,
        })
    return out


async def auto_search_comparables(db: AsyncSession, req: CmaCompSearchRequest, *, today: Optional[date] = None) -> dict:
    """The comp step's first, automatic search: one query at the widest window this subject could
    need, every candidate scored once, then plan_auto_search picks the radius and sold window. The
    price band is a loose 0.5x-2x of the asking price - a +/-35% band anchored on asking skewed the
    pool for over- or under-priced listings."""
    from app.services.cma_ai_service import _type_family as _tf
    today = today or date.today()
    if req.subject is not None and is_lease_listing(req.subject):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="This is a rental listing - a CMA values a sale, so there are no sale comps to find for it.")
    family = _tf(req.property_type or (req.subject.property_type if req.subject else None))
    # Fetch only as far as the main ladder goes (5 mi for a house); the rural 8-10 mi steps are
    # fetched separately, only when 5 mi wasn't enough - one 10-mile fetch for every house made
    # the comp step take 3-4 s on the live data (10/21).
    widest = {"condo": AUTO_RADII["urban"][-1], "attached": AUTO_RADII["attached"][-1]}.get(family, AUTO_RADII["detached"][-1])
    lp = (req.subject.list_price if req.subject else None) or None
    date_to = _parse_date(req.sale_date_to) or today
    wide = req.model_copy(update={
        "radius_miles": widest, "statuses": ["sold"],
        "sale_date_from": (date_to - timedelta(days=AUTO_SOLD_DAYS[-1])).isoformat(),
        "sale_date_to": req.sale_date_to,
        "min_price": round(lp * (1 - _AUTO_PRICE_BAND)) if lp else None, "max_price": round(lp * (1 + _AUTO_PRICE_BAND)) if lp else None,
    })
    pool = await search_comparables(db, wide, keep_all=True, row_limit=800)
    plan = plan_auto_search(pool, family, today=date_to, subject_year=(req.subject.year_built if req.subject else None))
    if plan["radius_miles"] > widest:   # rural: still short at 5 mi - look farther
        pool = await search_comparables(db, wide.model_copy(update={"radius_miles": AUTO_RURAL_EXTRA_RADII[-1]}),
                                        keep_all=True, row_limit=800)
        plan = plan_auto_search(pool, family, today=date_to, subject_year=(req.subject.year_built if req.subject else None))
    chosen = [c for c in pool
              if (c.distance_miles or 0) <= plan["radius_miles"]
              and (_sale_age_days(c, date_to) or 0) <= plan["sold_within_days"]]
    return {"results": chosen[:60], **plan,
            "price_band": [wide.min_price, wide.max_price],
            "limited_message": "Limited comparable sales - agent review recommended." if plan["limited"] else None}


async def drop_subject_from_comps(db: AsyncSession, owner_id: str, report_id: str) -> int:
    """Safety net before a (paid) AI run: a CMA saved before the comp search learned to exclude the
    subject's own earlier sales can still list the subject as one of its comps. Removes any comp at
    the subject's street address + ZIP (or the subject's listing). Returns how many were removed."""
    from app.models.database import CmaReport
    report = (await db.execute(select(CmaReport).where(CmaReport.id == report_id, CmaReport.owner_id == owner_id))).scalar_one_or_none()
    if not report or not isinstance(report.comparables, list):
        return 0
    subj = report.subject or {}
    norm = lambda v: " ".join(str(v or "").upper().replace(".", "").split())
    street, zip5, lk = norm(subj.get("street_address")), str(subj.get("zipcode") or "")[:5], subj.get("listing_key")
    if not street and not lk:
        return 0
    kept = [c for c in report.comparables if not (
        (lk and isinstance(c, dict) and c.get("listing_key") == lk)
        or (street and isinstance(c, dict) and norm(c.get("street_address")) == street and (not zip5 or str(c.get("zipcode") or "")[:5] in ("", zip5)))
    )]
    removed = len(report.comparables) - len(kept)
    if removed:
        report.comparables = kept
        await db.commit()
    return removed

