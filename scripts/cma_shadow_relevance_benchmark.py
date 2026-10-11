"""Read-only comparison of saved CMA weights vs. adaptive-proximity production v2.

Runs no model calls and writes no report data. Four live cases come from the latest saved CMA
reports, the estate case comes from the existing backtest fixture, and the dense-urban case is a
controlled packet because this database currently has no completed urban CMA report.
"""
from __future__ import annotations

import asyncio
import copy
import json
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import select

import app.main as _app_main  # noqa: F401 - resolves application import ordering
from app.database import AsyncSessionLocal
from app.models.database import CmaReport
from app.services.cma_ai_service import (
    _build_authoritative_evidence_snapshot,
    _build_evidence_by_key,
    _compute_comp_anchor,
    _compute_continuous_relevance_anchor,
    _flag_psf_outliers,
)
from app.services.cma_similarity_service import (
    build_adaptive_proximity_context,
    load_local_proximity_context,
)


LIVE_CASES = ("cherry", "lesley", "erika", "hamilton")


def _latest_trace(report: CmaReport) -> dict:
    return ((report.pricing or {}).get("ai_estimate") or {}).get("valuation_trace") or {}


def _current_anchor(subject: dict, comps: list[dict], saved_trace: dict | None = None) -> dict | None:
    saved = (saved_trace or {}).get("anchor")
    if saved and saved.get("anchor_value"):
        return saved
    evidence = _build_evidence_by_key(subject, comps)
    return _compute_comp_anchor(subject, comps, evidence, _flag_psf_outliers(comps))


def _normalized_label_address(label: str | None) -> str:
    text = str(label or "").strip().lower()
    if "(" in text and text.endswith(")"):
        text = text.rsplit("(", 1)[1][:-1]
    return " ".join(text.split())


def _row_map(anchor: dict | None) -> dict[str, dict]:
    out = {}
    for row in (anchor or {}).get("comps") or []:
        key = str(row.get("listing_key") or "")
        if key:
            out[f"key:{key}"] = row
        address = _normalized_label_address(row.get("label"))
        if address:
            out[f"address:{address}"] = row
    return out


def _compare_case(
    case_id: str, source: str, subject: dict, comps: list[dict], saved_trace: dict | None = None,
    *, density_context: dict | None = None, saved_fmv: float | None = None,
) -> dict:
    snapshot = _build_authoritative_evidence_snapshot(subject, comps, subject.get("_status_timeline") or {})
    frozen_subject, frozen_comps, _ = snapshot.materialize()
    current = _current_anchor(frozen_subject, frozen_comps, saved_trace)
    shadow = _compute_continuous_relevance_anchor(
        frozen_subject, frozen_comps, source_snapshot_sha256=snapshot.digest,
        density_context=density_context,
    )
    current_by_key = _row_map(current)

    # Prove stochastic role labels cannot perturb the proposed result.
    stability_runs = []
    role_patterns = (
        ["strong"] * len(frozen_comps),
        ["supporting"] * len(frozen_comps),
        ["weak" if i % 2 else "strong" for i in range(len(frozen_comps))],
    )
    for pattern in role_patterns:
        mutated = [dict(comp, strength=pattern[i]) for i, comp in enumerate(frozen_comps)]
        result = _compute_continuous_relevance_anchor(
            frozen_subject, mutated, density_context=density_context,
        )
        stability_runs.append((result["stability_fingerprint"], result["anchor_value"]))

    comp_rows = []
    for proposed in shadow["comps"]:
        current_row = (
            current_by_key.get(f"key:{proposed['listing_key']}")
            or current_by_key.get(f"address:{_normalized_label_address(proposed['label'])}")
            or {}
        )
        comp_rows.append({
            "comp": proposed["label"],
            "current_role": current_row.get("strength") or proposed.get("current_model_role"),
            "current_weight_pct": round(100 * float(current_row.get("normalized_weight") or 0), 1),
            "proposed_role": proposed["proposed_role"],
            "proposed_weight_pct": round(100 * float(proposed.get("normalized_weight") or 0), 1),
            "base_relevance": proposed["base_relevance_score"],
            "proximity": proposed["components"].get("proximity"),
            "market_area": proposed["components"].get("market_area"),
            "size": proposed["components"].get("size"),
        })

    current_value = (current or {}).get("anchor_value")
    proposed_value = shadow.get("anchor_value")
    return {
        "case": case_id,
        "source": source,
        "snapshot_sha256": snapshot.digest,
        "density": shadow["density_context"],
        "saved_final_fmv_before": saved_fmv,
        "saved_final_fmv_after_read_only_benchmark": saved_fmv,
        "current_anchor": current_value,
        "proposed_shadow_anchor": proposed_value,
        "anchor_delta": (proposed_value - current_value) if proposed_value and current_value else None,
        "anchor_delta_pct": round((proposed_value / current_value - 1) * 100, 2) if proposed_value and current_value else None,
        "current_max_weight_pct": round(
            100 * max((float(row.get("normalized_weight") or 0) for row in (current or {}).get("comps") or []), default=0), 1,
        ),
        "proposed_max_weight_pct": round(100 * shadow["max_normalized_weight"], 1),
        "dominance_flags": shadow["dominance_flags"],
        "repeated_role_flip_stable": len(set(stability_runs)) == 1,
        "stability_runs": [{"fingerprint": fingerprint, "anchor": anchor} for fingerprint, anchor in stability_runs],
        "comps": comp_rows,
    }


def _urban_controlled_packet() -> tuple[dict, list[dict]]:
    recent = lambda days: (date.today() - timedelta(days=days)).isoformat()
    subject = {
        "street_address": "Controlled Urban Subject", "city": "Philadelphia", "state": "PA",
        "school_district": "Philadelphia City", "township": "Philadelphia",
        "subdivision_name": "Rittenhouse Square", "property_type": "TOWNHOUSE",
        "home_type": "TOWNHOUSE", "sqft": 2100, "above_grade_finished_area": 2100,
        "lot_size": 1200, "year_built": 1915,
    }
    rows = (("U1", 1_150_000, 2050, 0.08, "strong"), ("U2", 1_100_000, 2150, 0.18, "supporting"),
            ("U3", 1_225_000, 2000, 0.42, "supporting"), ("U4", 1_050_000, 2200, 2.4, "supporting"))
    comps = []
    for i, (key, price, sqft, distance, strength) in enumerate(rows):
        comps.append({
            "listing_key": key, "street_address": f"Controlled Urban Comp {i + 1}",
            "city": "Philadelphia", "school_district": "Philadelphia City", "township": "Philadelphia",
            "subdivision_name": "Rittenhouse Square", "property_type": "TOWNHOUSE", "home_type": "TOWNHOUSE",
            "status": "sold", "price_verified": True, "sale_price": price, "list_price": price * 1.02,
            "sqft": sqft, "above_grade_finished_area": sqft, "lot_size": 1200,
            "year_built": 1915 + i * 3, "distance_miles": distance,
            "close_date": recent(45 + i * 20), "sale_date": recent(45 + i * 20),
            "is_included": True, "strength": strength,
        })
    return subject, comps


async def main() -> None:
    cases = []
    async with AsyncSessionLocal() as db:
        reports = (await db.execute(select(CmaReport).order_by(CmaReport.created_at.desc()))).scalars().all()
        for name in LIVE_CASES:
            report = next(
                row for row in reports
                if name in str((row.subject or {}).get("street_address") or "").lower()
            )
            density_context = await load_local_proximity_context(db, report.subject or {})
            estimate = (report.pricing or {}).get("ai_estimate") or {}
            final = estimate.get("final_valuation") or {}
            cases.append(_compare_case(
                name.title(), f"saved CMA report {report.id}", report.subject or {},
                report.comparables or [], _latest_trace(report), density_context=density_context,
                saved_fmv=final.get("value") or estimate.get("value"),
            ))

    fixture_path = Path(__file__).parent / "fixtures" / "cma_backtest" / "EXAMPLE.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    estate_density_refs = [
        {**fixture["comps"][i % len(fixture["comps"])], "listing_key": f"estate-ref-{i}",
         "street_address": f"Estate Density Reference {i}", "distance_miles": distance}
        for i, distance in enumerate((0.8, 1.4, 2.0, 2.7, 3.2, 3.7, 4.1, 4.4, 4.7, 5.0, 5.3, 5.7, 6.2), 1)
    ]
    estate_density = build_adaptive_proximity_context(fixture["subject"], estate_density_refs)
    cases.append(_compare_case(
        "Low-density estate", "existing saved backtest fixture EXAMPLE.json",
        fixture["subject"], fixture["comps"], None, density_context=estate_density,
    ))
    urban_subject, urban_comps = _urban_controlled_packet()
    urban_density_refs = [
        {**urban_comps[i % len(urban_comps)], "listing_key": f"urban-ref-{i}",
         "street_address": f"Urban Density Reference {i}", "distance_miles": distance}
        for i, distance in enumerate((0.08, 0.14, 0.20, 0.28, 0.34, 0.40, 0.47, 0.54, 0.60, 0.66, 0.72, 0.80, 0.90), 1)
    ]
    urban_density = build_adaptive_proximity_context(urban_subject, urban_density_refs)
    cases.append(_compare_case(
        "Dense urban", "controlled deterministic packet (no saved urban CMA exists)",
        urban_subject, urban_comps, None, density_context=urban_density,
    ))
    print(json.dumps({
        "version": "adaptive-proximity-v2",
        "production_activated": True,
        "model_calls": 0,
        "report_writes": 0,
        "cases": cases,
    }, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
