"""AI-generated CMA analysis: given a report's already-picked subject + comps (already narrowed
and scored by the existing client-side match-score algorithm - see frontend/lib/matchScore.ts,
persisted onto each comp as match_score), asks Claude to write a short note per comp explaining
the price gap and propose one value estimate + range for the subject.

First vision (photo-reading) use case in this codebase. Follows the same reserve/reconcile cost-
guardrail pattern as area_enrichment_service.py, and the same forced-tool-call structured-output
pattern as area_research_service.py (a single "submit_cma_analysis" tool call, so the response is
already a parsed dict - no manual JSON-in-text parsing).

Deliberately non-destructive: results land in ai_commentary/ai_estimate/ai_analysis, never
overwriting the agent's own commentary/pricing fields - the frontend offers an explicit "Apply"
action for that.
"""
import asyncio
import base64
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import CmaReport
from app.services import cma_service
from app.services.ai.ai_config_service import ai_config_service
from app.services.ai.ai_orchestrator_service import _get_client
from app.services.ai.ai_rate_limit_service import (
    AIRateLimitExceeded, reconcile_budget, record_usage_event, reserve_cma_analysis,
)

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"

_SYSTEM_PROMPT = (
    "You are assisting a real estate agent by analyzing comparable sold properties against a "
    "subject property, for a Comparative Market Analysis (CMA). You will be given structured "
    "data for a subject property and several comparable sold properties, plus listing photos and "
    "(sometimes) a map/street-view image for each. "
    "Only reference facts present in the data or clearly visible in the images - never invent a "
    "specific number (price, square footage, sale date, feature) that isn't given to you. If an "
    "image shows a condition, finish quality, or location detail (e.g. busy road, cul-de-sac, "
    "backs to woods) worth noting, say so, but don't overstate what a photo can actually tell you. "
    "For each comp, write 2-4 sentences explaining why it likely sold for more or less than the "
    "subject would be expected to, referencing price per square foot, notable feature "
    "differences, and any condition/location signal visible in its photos. Then give one overall "
    "estimated value, a low-high range, a confidence level, and a short methodology summary for "
    "the SUBJECT property. Call submit_cma_analysis with your results - do not respond with plain "
    "text."
)

_SUBMIT_TOOL = {
    "name": "submit_cma_analysis",
    "description": "Submit the per-comp analysis notes and the final value estimate for the subject property.",
    "input_schema": {
        "type": "object",
        "properties": {
            "comps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "listing_key": {"type": "string"},
                        "note": {"type": "string", "description": "2-4 sentences on why this comp likely sold higher/lower than the subject"},
                    },
                    "required": ["listing_key", "note"],
                },
            },
            "subject_estimate": {
                "type": "object",
                "properties": {
                    "value": {"type": "number"},
                    "range_min": {"type": "number"},
                    "range_max": {"type": "number"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "methodology_summary": {"type": "string"},
                },
                "required": ["value", "range_min", "range_max", "confidence", "methodology_summary"],
            },
        },
        "required": ["comps", "subject_estimate"],
    },
}

# Fields pulled out of the subject/comp dicts for the text half of the prompt - deliberately just
# the fields already on CmaSubjectProperty/CmaComparable (schemas/cma.py), nothing new invented.
_SUBJECT_FIELDS = [
    "street_address", "city", "state", "zipcode", "list_price", "beds", "baths", "sqft",
    "lot_size", "year_built", "property_type", "garage", "basement", "days_on_market",
    "mls_status", "last_sale_date", "last_sale_price", "school_district", "annual_taxes",
]
_COMP_FIELDS = [
    "street_address", "city", "state", "zipcode", "distance_miles", "sale_price", "list_price",
    "sale_date", "days_on_market", "sqft", "lot_size", "beds", "baths", "year_built", "garage",
    "basement", "price_per_sqft", "annual_taxes", "match_score",
]


def _format_block(label: str, data: dict, fields: list[str]) -> str:
    lines = [label]
    for field in fields:
        value = data.get(field)
        if value not in (None, ""):
            lines.append(f"  {field}: {value}")
    return "\n".join(lines)


def _photo_urls(data: dict, max_photos: int) -> list[str]:
    photos = data.get("photos") or []
    if not photos and data.get("photo_url"):
        photos = [data["photo_url"]]
    return [p for p in photos[:max_photos] if p]


def _compute_fingerprint(subject: dict, comps: list[dict], max_photos: int, include_map_images: bool) -> str:
    payload = {
        "subject_key": subject.get("listing_key") or subject.get("street_address"),
        "comp_keys": sorted(c.get("listing_key") for c in comps if c.get("listing_key")),
        "max_photos": max_photos,
        "include_map_images": include_map_images,
        "prompt_version": PROMPT_VERSION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


async def _fetch_image_block(http: httpx.AsyncClient, url: str) -> Optional[dict]:
    """Downloads and base64-encodes one photo rather than handing Claude the raw URL - MLS media
    CDNs commonly disallow bot fetching in robots.txt, and Anthropic's url-source images respect
    that (confirmed: Bright's own CDN 400s with "disallowed by the website's robots.txt file").
    Fetching here and sending base64 sidesteps that entirely. Returns None (skip, don't fail the
    whole generation) on any download/content-type problem."""
    try:
        resp = await http.get(url, timeout=15.0)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        if not content_type.startswith("image/"):
            return None
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": content_type, "data": base64.b64encode(resp.content).decode()},
        }
    except Exception as exc:
        logger.info("Skipping unreachable CMA comp photo", extra={"url": url, "error": str(exc)})
        return None


async def _build_content(subject: dict, comps: list[dict], max_photos: int, warnings: list[str]) -> tuple[list[dict], dict]:
    """Builds the user message content blocks (text + image) and the images_sent transparency
    record. Text blocks first (all context up front), then images grouped subject-first."""
    content: list[dict] = [{"type": "text", "text": _format_block("SUBJECT PROPERTY:", subject, _SUBJECT_FIELDS)}]
    for comp in comps:
        content.append({"type": "text", "text": _format_block(f"COMPARABLE ({comp.get('listing_key', 'unknown')}):", comp, _COMP_FIELDS)})

    images_sent: dict = {}

    async with httpx.AsyncClient() as http:
        async def add_images(key: str, data: dict):
            urls = _photo_urls(data, max_photos)
            if not urls:
                warnings.append(f"No photos available for {key}")
                return
            blocks = [b for b in await asyncio.gather(*[_fetch_image_block(http, u) for u in urls]) if b]
            if not blocks:
                warnings.append(f"Photos for {key} couldn't be loaded")
                return
            content.append({"type": "text", "text": f"Photos for {key}:"})
            content.extend(blocks)
            images_sent[key] = urls[:len(blocks)]

        await add_images("subject", subject)
        for comp in comps:
            await add_images(comp.get("listing_key") or comp.get("street_address") or "comp", comp)

    content.append({
        "type": "text",
        "text": "Now analyze each comp against the subject and call submit_cma_analysis with your findings.",
    })
    return content, images_sent


async def generate_ai_analysis(
    db: AsyncSession, owner_id: str, report_id: str,
    max_photos_per_property: Optional[int] = None, include_map_images: bool = False,
    force_regenerate: bool = False,
) -> CmaReport:
    report = await cma_service._get_owned(db, owner_id, report_id)
    config = await ai_config_service.get_config(db)

    max_photos = min(
        max_photos_per_property or config["cma_ai_default_max_photos_per_property"],
        config["cma_ai_max_photos_per_property_cap"],
    )

    subject = report.subject or {}
    comps = [c for c in (report.comparables or []) if c.get("is_included", True)]
    if not comps:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Add at least one comparable before generating an AI analysis.")
    if subject.get("latitude") is None or subject.get("longitude") is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The subject property needs a location before generating an AI analysis.")

    fingerprint = _compute_fingerprint(subject, comps, max_photos, include_map_images)
    existing_meta = report.ai_analysis or {}
    if not force_regenerate and existing_meta.get("input_fingerprint") == fingerprint:
        return report

    reservation = await reserve_cma_analysis(db, owner_id, config)

    warnings: list[str] = []
    # include_map_images stays a documented no-op for now (Phase C - the app's Google key is
    # Places-API-restricted and Static Maps/Street View likely need a separate enablement check
    # before this can be wired in without risking every generation's images silently failing).
    if include_map_images:
        warnings.append("Map/street-view images are not yet available - analysis is based on listing photos and data only.")

    content, images_sent = await _build_content(subject, comps, max_photos, warnings)
    model = config["cma_ai_model"]

    try:
        client = _get_client()
        response = await client.messages.create(
            model=model,
            max_tokens=config["cma_ai_max_output_tokens"],
            system=_SYSTEM_PROMPT,
            tools=[_SUBMIT_TOOL],
            tool_choice={"type": "tool", "name": "submit_cma_analysis"},
            messages=[{"role": "user", "content": content}],
        )
    except Exception as exc:
        logger.warning("CMA AI analysis call failed", extra={"report_id": report_id, "error": str(exc)})
        await reconcile_budget(db, reservation, 0.0)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't generate the AI analysis right now. Try again in a moment.")

    submit_block = next((b for b in response.content if getattr(b, "type", None) == "tool_use" and b.name == "submit_cma_analysis"), None)
    result = submit_block.input if submit_block and isinstance(submit_block.input, dict) else None

    cost = ai_config_service.estimate_cost_usd(config, model, response.usage.input_tokens, response.usage.output_tokens)
    await record_usage_event(
        db, owner_id, None, model, response.usage.input_tokens, response.usage.output_tokens,
        cost, 0, False, None, event_type="cma_ai",
    )
    await reconcile_budget(db, reservation, cost)

    if not result:
        warnings.append("The AI didn't return a usable result - try regenerating.")
        result = {"comps": [], "subject_estimate": None}

    notes_by_key = {c.get("listing_key"): c.get("note") for c in (result.get("comps") or []) if c.get("listing_key")}
    new_comparables = []
    for comp in report.comparables or []:
        comp = dict(comp)
        note = notes_by_key.get(comp.get("listing_key"))
        if note:
            comp["ai_commentary"] = note
        new_comparables.append(comp)
    report.comparables = new_comparables

    subject_estimate = result.get("subject_estimate")
    if subject_estimate:
        new_pricing = dict(report.pricing or {})
        new_pricing["ai_estimate"] = {**subject_estimate, "model": model, "generated_at": datetime.now(timezone.utc).isoformat()}
        report.pricing = new_pricing
    else:
        warnings.append("The AI didn't return a value estimate - try regenerating.")

    report.ai_analysis = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "cost_usd": round(cost, 4),
        "input_fingerprint": fingerprint,
        "images_sent": images_sent,
        "warnings": warnings,
    }
    await db.commit()
    await db.refresh(report)
    return report
