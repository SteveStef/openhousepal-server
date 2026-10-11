"""Read-only adaptive-similarity explanation for one subject/target pair."""
from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import func, select

import app.main as _app_main  # noqa: F401
from app.database import AsyncSessionLocal
from app.models.database import CmaReport, Property
from app.schemas.cma import CmaCompSearchRequest
from app.services.cma_ai_service import _continuous_relevance
from app.services.cma_service import _categorize_status, search_comparables
from app.services.cma_similarity_service import load_local_proximity_context
from app.utils.geo import haversine_distance


async def run(subject_text: str, target_text: str) -> None:
    async with AsyncSessionLocal() as db:
        reports = (await db.execute(select(CmaReport).order_by(CmaReport.created_at.desc()))).scalars().all()
        report = next(
            row for row in reports
            if subject_text.lower() in str((row.subject or {}).get("street_address") or "").lower()
        )
        subject = report.subject or {}
        sqft = subject.get("above_grade_finished_area") or subject.get("sqft")
        candidates = await search_comparables(db, CmaCompSearchRequest(
            listing_key=subject.get("listing_key"), latitude=subject.get("latitude"),
            longitude=subject.get("longitude"), subject_sqft=sqft,
            property_type=subject.get("property_type"), radius_miles=10,
            statuses=["sold", "pending", "active_under_contract", "active"],
            min_sqft=sqft * 0.65 if sqft else None, max_sqft=sqft * 1.35 if sqft else None,
        ))
        subject_address = " ".join(str(subject.get("street_address") or "").lower().split())
        rows = [
            candidate.model_dump() for candidate in candidates
            if " ".join(str(candidate.street_address or "").lower().split()) != subject_address
        ]
        target_query = select(Property)
        for token in target_text.lower().split():
            target_query = target_query.where(func.lower(Property.street_address).contains(token))
        target_property = (await db.execute(
            target_query.order_by(Property.updated_at.desc().nullslast()).limit(1)
        )).scalar_one_or_none()
        if target_property and not any(row["listing_key"] == target_property.listing_key for row in rows):
            raw = target_property.raw_mls_data or {}
            category = _categorize_status(target_property.home_status) or "sold"
            close_price = raw.get("ClosePrice")
            try:
                close_price = float(close_price) if close_price else None
            except (TypeError, ValueError):
                close_price = None
            rows.append({
                "listing_key": target_property.listing_key,
                "street_address": target_property.street_address,
                "city": target_property.city, "state": target_property.state,
                "zipcode": target_property.zipcode,
                "latitude": target_property.latitude, "longitude": target_property.longitude,
                "distance_miles": haversine_distance(
                    subject["latitude"], subject["longitude"],
                    target_property.latitude, target_property.longitude,
                ),
                "status": category, "price_verified": bool(category == "sold" and close_price),
                "sale_price": close_price, "close_price": close_price,
                "list_price": target_property.price,
                "close_date": raw.get("CloseDate"), "sale_date": raw.get("CloseDate"),
                "sqft": target_property.living_area,
                "above_grade_finished_area": target_property.above_grade_finished_area,
                "below_grade_finished_area": target_property.below_grade_finished_area,
                "lot_size": target_property.lot_size, "year_built": target_property.year_built,
                "property_type": target_property.home_type, "home_type": target_property.home_type,
                "school_district": raw.get("SchoolDistrictName") or target_property.school_district_name,
                "public_remarks": raw.get("PublicRemarks"), "is_included": True,
            })
        context = await load_local_proximity_context(db, subject)
        relevance = _continuous_relevance(subject, rows, density_context=context)
        ranked = sorted(relevance["comps"], key=lambda row: row["adjusted_relevance_score"], reverse=True)
        target = next(
            (row for row in ranked if target_text.lower() in str(row.get("label") or "").lower()),
            None,
        )
        legacy = _continuous_relevance(subject, rows, density_context={
            "version": "fixed-proximity-v1", "band": "fixed",
            "distance_half_life_miles": 2.5, "local_market_half_life_miles": 2.5,
            "reference_count": 0, "density_distances_used_miles": [], "fallback_used": False,
        })
        legacy_ranked = sorted(legacy["comps"], key=lambda row: row["adjusted_relevance_score"], reverse=True)
        legacy_target = next(
            (row for row in legacy_ranked if target_text.lower() in str(row.get("label") or "").lower()),
            None,
        )
        print(json.dumps({
            "report_id": report.id,
            "subject": subject.get("street_address"),
            "target_query": target_text,
            "candidate_pool_size": len(ranked),
            "target_rank": ranked.index(target) + 1 if target else None,
            "target": target,
            "fixed_2_5_mile_before": {
                "target_rank": legacy_ranked.index(legacy_target) + 1 if legacy_target else None,
                "target_score": round(legacy_target["adjusted_relevance_score"] * 100) if legacy_target else None,
                "proximity_points": legacy_target.get("proximity_score_points") if legacy_target else None,
            },
            "density_context": context,
            "top_10": [
                {"rank": index + 1, "comp": row["label"],
                 "score": round(row["adjusted_relevance_score"] * 100),
                 "distance": row["candidate_distance_miles"]}
                for index, row in enumerate(ranked[:10])
            ],
        }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("subject")
    parser.add_argument("target")
    args = parser.parse_args()
    asyncio.run(run(args.subject, args.target))


if __name__ == "__main__":
    main()
