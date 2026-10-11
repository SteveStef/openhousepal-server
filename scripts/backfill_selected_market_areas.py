"""Backfill/report only canonical MarketArea rows currently selected by agents.

This intentionally follows the shared MarketArea/AgentMarketArea relationships instead of a
hard-coded test account. It reuses current, usable v6 research and only invokes the existing
v6 enrichment pipeline for stale, failed, missing, or materially incomplete current rows.
"""
import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

import app.main  # noqa: F401 - loads app configuration/env the same way the server does.
from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models.database import AIUsageEvent, AgentMarketArea, MarketArea, User
from app.services.areas import area_enrichment_service, area_research_service, geography

MIN_USABLE_RESEARCH_CATEGORIES = 7


class AreaResearchLogCapture(logging.Handler):
    def __init__(self, area_id: str):
        super().__init__()
        self.area_id = area_id
        self.calls: list[dict[str, Any]] = []
        self.total_cost_usd = 0.0

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "area_id", None) != self.area_id:
            return
        if record.getMessage() != "Grouped AI area research call breakdown":
            return
        self.calls = list(getattr(record, "calls", []) or [])
        self.total_cost_usd = float(getattr(record, "total_cost_usd", 0.0) or 0.0)


@dataclass
class SelectedAreaRun:
    area: MarketArea
    triggering_user_id: str
    selected_count: int
    research_status: str
    calls: int = 0
    searches: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
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


def _has_current_usable_research(area: MarketArea) -> bool:
    if area.research_version != area_research_service.CURRENT_RESEARCH_VERSION:
        return False
    if area.enrichment_status == "ENRICHED":
        return True
    if area.enrichment_status == "PARTIAL":
        complete, _total = _categories_complete(area)
        return complete >= MIN_USABLE_RESEARCH_CATEGORIES and bool(area.facts)
    return False


def _should_research(area: MarketArea) -> bool:
    if area_enrichment_service.needs_research(area):
        return True
    return not _has_current_usable_research(area)


async def _selected_areas(db) -> list[SelectedAreaRun]:
    rows = (
        await db.execute(
            select(
                MarketArea,
                func.min(AgentMarketArea.owner_id).label("triggering_user_id"),
                func.count(AgentMarketArea.id).label("selected_count"),
            )
            .join(AgentMarketArea, AgentMarketArea.area_id == MarketArea.id)
            .group_by(MarketArea.id)
            .order_by(MarketArea.state, MarketArea.name)
        )
    ).all()
    return [
        SelectedAreaRun(
            area=area,
            triggering_user_id=triggering_user_id,
            selected_count=int(selected_count or 0),
            research_status="PENDING",
        )
        for area, triggering_user_id, selected_count in rows
    ]


async def _event_usage_since(db, owner_id: str, started_at: datetime) -> dict[str, float | int]:
    events = (
        await db.execute(
            select(AIUsageEvent).where(
                AIUsageEvent.owner_id == owner_id,
                AIUsageEvent.event_type == area_research_service.AREA_RESEARCH_EVENT_TYPE,
                AIUsageEvent.blocked.is_(False),
                AIUsageEvent.created_at >= started_at,
            )
        )
    ).scalars().all()
    return {
        "input_tokens": sum(event.input_tokens or 0 for event in events),
        "output_tokens": sum(event.output_tokens or 0 for event in events),
        "cost_usd": sum(event.estimated_cost_usd or 0.0 for event in events),
        "event_count": len(events),
    }


async def _run_research(db, result: SelectedAreaRun) -> None:
    user = (await db.execute(select(User).where(User.id == result.triggering_user_id))).scalar_one()
    capture = AreaResearchLogCapture(result.area.id)
    logger = logging.getLogger("app.services.areas.area_enrichment_service")
    logger.addHandler(capture)
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    try:
        result.area = await area_enrichment_service.enrich_area(db, result.area, user)
    finally:
        result.elapsed_s = time.perf_counter() - started
        logger.removeHandler(capture)

    result.research_status = f"RAN -> {result.area.enrichment_status}"
    if capture.calls:
        result.calls = len(capture.calls)
        result.searches = sum(int(call.get("web_search_requests") or 0) for call in capture.calls)
        result.input_tokens = sum(int(call.get("input_tokens") or 0) for call in capture.calls)
        result.output_tokens = sum(int(call.get("output_tokens") or 0) for call in capture.calls)
        result.cost_usd = capture.total_cost_usd
        return

    usage = await _event_usage_since(db, result.triggering_user_id, started_at)
    result.calls = int(usage["event_count"])
    result.searches = 0
    result.input_tokens = int(usage["input_tokens"])
    result.output_tokens = int(usage["output_tokens"])
    result.cost_usd = float(usage["cost_usd"])


def _format_money(value: float) -> str:
    return f"${value:.6f}"


def _row(result: SelectedAreaRun) -> list[str]:
    area = result.area
    scope = geography.scope_for_market_area(area)
    complete, total = _categories_complete(area)
    return [
        area.name,
        area.area_type,
        area.parent_area_name or "",
        result.research_status,
        str(area.research_version or ""),
        scope.get("kind", "radius"),
        f"{complete}/{total}",
        str(result.calls),
        str(result.searches),
        str(result.input_tokens),
        str(result.output_tokens),
        _format_money(result.cost_usd),
        f"{result.elapsed_s:.1f}s",
    ]


def _print_table(results: list[SelectedAreaRun]) -> None:
    headers = [
        "Area",
        "Type",
        "Parent",
        "Research status",
        "Research version",
        "Geography type",
        "Categories complete",
        "Calls",
        "Searches",
        "Input tokens",
        "Output tokens",
        "Cost",
        "Time",
    ]
    print("| " + " | ".join(headers) + " |")
    print("| " + " | ".join("---" for _ in headers) + " |")
    for result in results:
        print("| " + " | ".join(_row(result)) + " |")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    async with AsyncSessionLocal() as db:
        results = await _selected_areas(db)
        for result in results:
            if _should_research(result.area):
                await _run_research(db, result)
            else:
                result.research_status = "REUSED"

        _print_table(results)
        print()

        total_cost = sum(result.cost_usd for result in results)
        highest = max(results, key=lambda result: result.cost_usd, default=None)
        reused = [result.area.name for result in results if result.cost_usd == 0]
        approximate = [
            result.area.name
            for result in results
            if geography.scope_for_market_area(result.area).get("kind") in {"bounds", "radius"}
        ]
        failures = [
            result.area.name
            for result in results
            if result.area.enrichment_status in {"FAILED", "PARTIAL"}
        ]
        print(f"Total selected canonical areas: {len(results)}")
        print(f"Total cost: {_format_money(total_cost)}")
        print(f"Average cost per area: {_format_money(total_cost / len(results) if results else 0.0)}")
        if highest and highest.cost_usd > 0:
            print(f"Highest-cost area: {highest.area.name} ({_format_money(highest.cost_usd)})")
        else:
            print("Highest-cost area: none (all reused existing shared research)")
        print(f"Failures/partials: {', '.join(failures) if failures else 'none'}")
        print(f"Reused existing shared research ($0): {', '.join(reused) if reused else 'none'}")
        print(f"Approximate geography instead of polygons: {', '.join(approximate) if approximate else 'none'}")


if __name__ == "__main__":
    asyncio.run(main())
