import secrets
import string
import hashlib
import json
import re
from datetime import datetime, date, timezone
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
from app.utils.listing_dom import resolve_days_on_market


_AUTHORITATIVE_SUBJECT_FACT_FIELDS = (
    "above_grade_finished_area",
    "below_grade_finished_area",
    "architectural_style",
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


def _property_subject_facts(prop: Property) -> Dict[str, Any]:
    """One MLS-backed source for hard subject facts used by every match calculation."""
    raw = prop.raw_mls_data or {}
    return {
        "above_grade_finished_area": prop.above_grade_finished_area,
        "below_grade_finished_area": prop.below_grade_finished_area,
        "architectural_style": prop.architectural_style,
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


def _merge_authoritative_subject_facts(subject: Dict[str, Any], prop: Property) -> Dict[str, Any]:
    """Fill absent MLS facts without overwriting an agent's explicit subject correction."""
    merged = dict(subject or {})
    for field, value in _property_subject_facts(prop).items():
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


_GENERATION_STALE_AFTER_SECONDS = 480  # generous vs. real generation time (~30-90s) - a request
# that's still marked "generating" past this almost certainly crashed/restarted mid-run rather
# than genuinely still being in flight, so stop showing the client a spinner for it.


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
    for requests that never got that far (e.g. failed validation before any AI call)."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(CmaReport)
            .where(CmaReport.id == report_id, CmaReport.owner_id == owner_id, CmaReport.generation_status == "generating")
            .values(generation_status=outcome, generation_error=(error or None) if outcome != "complete" else None)
        )
        await session.commit()


async def list_reports(db: AsyncSession, owner_id: str) -> List[CmaReportListItem]:
    result = await db.execute(
        select(CmaReport)
        .where(CmaReport.owner_id == owner_id)
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
        data["subject"] = await _canonical_subject_snapshot(db, data["subject"] or {})
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


async def search_comparables(db: AsyncSession, req: CmaCompSearchRequest) -> List[CmaCompCandidate]:
    """Finds candidate comparables around the subject from the local MLS mirror. This suggests -
    it never decides - which properties belong in the CMA; the agent picks from these results."""
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
    )
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
    result = await db.execute(query.order_by(distance_expr.asc()).limit(400))
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
            baths=p.bathrooms,
            year_built=p.year_built,
            architectural_style=p.architectural_style,
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
    # Same housing product first (a twin's comps are twins), nearest-first within each group - the
    # grid only shows 60, and in a dense area 60 nearest-of-anything can hold almost no twins.
    from app.services.cma_ai_service import _type_family
    _subject_family = _type_family(req.property_type or (req.subject.property_type if req.subject else None))
    if _subject_family:
        candidates.sort(key=lambda c: _type_family(c.property_type) not in (None, _subject_family))
    visible_candidates = [
        candidate.model_copy(update={"similarity_context": similarity_context})
        for candidate in candidates[:60]
    ]

    return _score_comparable_candidates(canonical_subject, visible_candidates, similarity_context)


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

