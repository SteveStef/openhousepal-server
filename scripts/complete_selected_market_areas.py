"""Targeted completion for currently selected MarketArea rows.

This is intentionally narrower than a full re-enrichment: it inspects the stored v6 facts for
canonical areas selected by agents, repairs only unresolved or obviously weak categories with the
existing v6 solo-category completion call, and verifies that public community pages can render
current shared facts plus live OHP inventory.
"""
import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

import app.main  # noqa: F401
from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models.database import AgentMarketArea, MarketArea, User
from app.services.ai.ai_rate_limit_service import record_usage_event
from app.services.areas import area_enrichment_service, area_research_service, geography
from app.services.website import content_adapters

TARGETED_REPAIR_EMPTY_CATEGORIES = {"medical"}


@dataclass
class CompletionResult:
    area: MarketArea
    triggering_user_id: str
    repaired: list[str] = field(default_factory=list)
    calls: int = 0
    searches: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    inventory_total: int = 0
    public_page_ok: bool = False
    manual_review: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0


def _categories_complete(area: MarketArea) -> tuple[int, int]:
    facts = area.facts or {}
    total = len(area_research_service.RESEARCH_CATEGORIES)
    complete = sum(
        1
        for category in area_research_service.RESEARCH_CATEGORIES
        if area_research_service.category_is_resolved(category, facts.get(category) or {})
    )
    return complete, total


def _has_content(category: str, value: dict) -> bool:
    return area_research_service.result_has_content(category, value or {})


def _categories_to_repair(area: MarketArea) -> list[str]:
    facts = area.facts or {}
    categories: list[str] = []
    for category in area_research_service.RESEARCH_CATEGORIES:
        value = facts.get(category) or {}
        if not area_research_service.category_is_resolved(category, value):
            categories.append(category)
            continue
        if category in TARGETED_REPAIR_EMPTY_CATEGORIES and not _has_content(category, value):
            categories.append(category)
    return categories


def _secondary_evidence(area: MarketArea) -> str:
    facts = area.facts or {}
    school_districts = (facts.get("market_stats") or {}).get("school_districts") or []
    nces_evidence = facts.get("schools") or []
    nces_summary = ", ".join(f"{s.get('level')}: {s.get('name')}" for s in nces_evidence) or "(none available)"
    districts_summary = ", ".join(school_districts) or "(none available)"
    return (
        "This platform's own EXISTING evidence for this community, for use ONLY as a secondary cross-check AFTER "
        "your own independent web research - this may be wrong, incomplete, or stale, so do not treat it as a "
        "starting point or let it override what your current research finds:\n"
        f"- School district name(s) recorded on past MLS listings in this community's ZIP codes: {districts_summary}\n"
        f"- An older attendance-boundary lookup for this community's centroid point: {nces_summary}"
    )


async def _selected_areas(db) -> list[CompletionResult]:
    rows = (
        await db.execute(
            select(MarketArea, func.min(AgentMarketArea.owner_id).label("triggering_user_id"))
            .join(AgentMarketArea, AgentMarketArea.area_id == MarketArea.id)
            .group_by(MarketArea.id)
            .order_by(MarketArea.state, MarketArea.name)
        )
    ).all()
    return [CompletionResult(area=area, triggering_user_id=triggering_user_id) for area, triggering_user_id in rows]


async def _repair_category(db, result: CompletionResult, user: User, category: str) -> None:
    raw, usage = await area_research_service.run_solo_category_research(
        db,
        result.area,
        user,
        category,
        _secondary_evidence(result.area),
        max_searches=3,
    )
    result.calls += 1
    result.searches += int(usage.get("web_search_requests") or 0)
    result.input_tokens += int(usage.get("input_tokens") or 0)
    result.output_tokens += int(usage.get("output_tokens") or 0)
    result.cost_usd += float(usage.get("estimated_cost_usd") or 0.0)

    await record_usage_event(
        db,
        user.id,
        None,
        usage.get("model"),
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        usage.get("estimated_cost_usd"),
        0,
        raw is None,
        "no_submission" if raw is None else None,
        event_type=area_research_service.AREA_RESEARCH_EVENT_TYPE,
    )
    if raw is None:
        result.manual_review.append(f"{category}: no structured result")
        return

    cleaned, dropped = area_research_service.validate_category_result(category, (raw or {}).get(category) or {})
    facts = dict(result.area.facts or {})
    area_enrichment_service._write_category_facts(facts, category, cleaned, dropped, usage, escalated=False)
    result.area.facts = facts
    result.repaired.append(category)
    if not area_research_service.category_is_resolved(category, cleaned):
        result.manual_review.append(f"{category}: still unresolved after targeted completion")


async def _verify_public_page_inputs(db, result: CompletionResult) -> None:
    if not (result.area.narrative or {}).get("summary"):
        result.manual_review.append("missing narrative")
    listings, total = await content_adapters.get_area_inventory(
        db,
        geography.scope_for_market_area(result.area),
        limit=1,
        offset=0,
    )
    result.inventory_total = total
    if total <= 0:
        result.manual_review.append("no live community inventory")
    complete, total_categories = _categories_complete(result.area)
    result.public_page_ok = (
        complete == total_categories
        and bool((result.area.narrative or {}).get("summary"))
        and result.inventory_total > 0
    )


async def _process_area(db, result: CompletionResult) -> None:
    started = time.perf_counter()
    user = (await db.execute(select(User).where(User.id == result.triggering_user_id))).scalar_one()
    for category in _categories_to_repair(result.area):
        await _repair_category(db, result, user, category)

    result.area.research_version = area_research_service.CURRENT_RESEARCH_VERSION
    result.area.enrichment_status = area_enrichment_service.enrichment_status_for_facts(result.area.facts or {})
    db.add(result.area)
    await db.commit()
    await db.refresh(result.area)

    await _verify_public_page_inputs(db, result)
    result.elapsed_s = time.perf_counter() - started


def _money(value: float) -> str:
    return f"${value:.6f}"


def _print_report(results: list[CompletionResult]) -> None:
    headers = [
        "Area",
        "Final status",
        "Categories complete",
        "Categories repaired",
        "Research calls",
        "Searches",
        "Cost",
        "Geography type",
        "Public page OK",
    ]
    print("| " + " | ".join(headers) + " |")
    print("| " + " | ".join("---" for _ in headers) + " |")
    for result in results:
        complete, total = _categories_complete(result.area)
        print("| " + " | ".join([
            result.area.name,
            result.area.enrichment_status,
            f"{complete}/{total}",
            ", ".join(result.repaired) or "none",
            str(result.calls),
            str(result.searches),
            _money(result.cost_usd),
            geography.scope_for_market_area(result.area).get("kind", "radius"),
            f"{'yes' if result.public_page_ok else 'no'} ({result.inventory_total} listings)",
        ]) + " |")
    print()
    total_cost = sum(result.cost_usd for result in results)
    repaired_results = [result for result in results if result.repaired]
    partials = [result.area.name for result in results if result.area.enrichment_status == "PARTIAL"]
    manual = [
        f"{result.area.name}: {', '.join(result.manual_review)}"
        for result in results
        if result.manual_review
    ]
    print(f"Total additional cost: {_money(total_cost)}")
    print(f"Average cost per repaired area: {_money(total_cost / len(repaired_results) if repaired_results else 0.0)}")
    print(f"Remaining partials: {', '.join(partials) if partials else 'none'}")
    print(f"Manual review: {'; '.join(manual) if manual else 'none'}")


async def main() -> None:
    async with AsyncSessionLocal() as db:
        results = await _selected_areas(db)
        for result in results:
            await _process_area(db, result)
        _print_report(results)


if __name__ == "__main__":
    asyncio.run(main())
