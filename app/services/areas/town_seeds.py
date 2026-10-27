"""Finished towns shipped with the code (owner, 10/26: "the towns that we have fully done, push them
to the main site so we do not have to redo them").

Each file in app/data/town_seeds/ is one researched-and-checked town record exported from a local
research batch. On start-up apply_town_seeds() puts each one on this server once:
  - a town that isn't here yet is created (so the first agent who adds it gets it instantly, free)
  - a town that's here but never had the new research + place check gets the finished version
  - a town that already has the new research, or that an admin has edited, is never overwritten
Agents' links to towns are untouched - a seed is only the shared town record.
"""
from __future__ import annotations

import gzip
import json
import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.database import MarketArea

logger = logging.getLogger(__name__)

SEED_DIR = Path(__file__).resolve().parents[2] / "data" / "town_seeds"
# The town record's own columns a seed carries (never id, created_at, or agent links).
SEED_COLUMNS = ("name", "state", "slug", "canonical_key", "county", "municipality_type", "area_type",
                "parent_area_name", "zip_codes", "centroid_lat", "centroid_lng", "geography", "facts",
                "narrative", "enrichment_status", "research_version", "last_enriched_at", "last_verified_at")
_DATES = ("last_enriched_at", "last_verified_at")


def export_seed(area: MarketArea, seed_id: str) -> dict:
    row = {c: getattr(area, c) for c in SEED_COLUMNS}
    for c in _DATES:
        row[c] = row[c].isoformat() if row[c] else None
    return {"seed_id": seed_id, "town": row}


def load_seeds(seed_dir: Path = SEED_DIR) -> list[dict]:
    out = []
    for path in sorted(seed_dir.glob("*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            out.append(json.load(f))
    return out


def _finished_here(area: MarketArea) -> bool:
    facts = area.facts or {}
    return bool(facts.get("_places_check_version")) or any(
        e.get("admin_edited") for k, v in facts.items() if isinstance(v, dict)
        for e in (v.get("entities") or []) if isinstance(e, dict)) or bool(facts.get("_admin_edited_at"))


def _admin_edited(area: MarketArea) -> bool:
    facts = area.facts or {}
    return bool(facts.get("_admin_edited_at")) or any(
        e.get("admin_edited") for k, v in facts.items() if isinstance(v, dict)
        for e in (v.get("entities") or []) if isinstance(e, dict))


def _older_seed_untouched(area: MarketArea, seed_id: str) -> bool:
    """This server's copy came from an older seed (seed ids are dates, "2026-10-26") and no admin
    has edited it since."""
    older = (area.facts or {}).get("_seeded_from")
    return bool(older) and str(older) < str(seed_id) and not _admin_edited(area)


def _live_research_untouched(area: MarketArea) -> bool:
    """A town this server researched itself (an agent added it - Kennett Square, Landenberg) that a
    seed marked replaces_live_research may update: the seed is that same research brought here,
    with the later fixes. Never once an admin has edited it."""
    return not (area.facts or {}).get("_seeded_from") and not _admin_edited(area)


def _fill(area: MarketArea, town: dict, seed_id: str, keep_identity: bool) -> None:
    for c in SEED_COLUMNS:
        if keep_identity and c in ("slug", "canonical_key", "name"):
            continue  # the live row keeps the address agents' pages already link to
        value = town.get(c)
        if c in _DATES and value:
            value = datetime.fromisoformat(value)
        setattr(area, c, value)
    facts = dict(area.facts or {})
    facts["_seeded_from"] = seed_id
    area.facts = facts
    flag_modified(area, "facts")


async def apply_town_seeds(db: AsyncSession, seeds: list[dict] | None = None) -> dict:
    summary = {"created": 0, "filled": 0, "updated": 0, "kept": 0}
    for seed in seeds if seeds is not None else load_seeds():
        town, seed_id = seed["town"], seed["seed_id"]
        try:
            area = (await db.execute(select(MarketArea).where(
                (MarketArea.canonical_key == town["canonical_key"]) | (MarketArea.slug == town["slug"])))).scalars().first()
            if area is None:
                area = MarketArea()
                _fill(area, town, seed_id, keep_identity=False)
                db.add(area)
                summary["created"] += 1
            elif (area.facts or {}).get("_seeded_from") == seed_id:
                summary["kept"] += 1
                continue
            elif _older_seed_untouched(area, seed_id) or (seed.get("replaces_live_research") and _live_research_untouched(area)):
                # A newer finished version of a town this server got from an older seed (10/27 grader
                # fixes) - replaces it, unless an admin has edited the town here since.
                _fill(area, town, seed_id, keep_identity=True)
                db.add(area)
                summary["updated"] += 1
            elif _finished_here(area):
                summary["kept"] += 1
                continue
            else:
                _fill(area, town, seed_id, keep_identity=True)
                db.add(area)
                summary["filled"] += 1
            await db.commit()
        except IntegrityError:  # another worker created it a moment ago
            await db.rollback()
            summary["kept"] += 1
    return summary


async def scheduled_apply_town_seeds() -> None:
    from app.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            result = await apply_town_seeds(db)
        logger.info("Town seeds applied: %s", result)
    except Exception:
        logger.exception("applying town seeds failed")
