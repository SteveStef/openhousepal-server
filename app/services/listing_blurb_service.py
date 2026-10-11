"""The short "about this home" lines Studio designs print, one per agent per listing.

First design for a home: the cheap AI model writes 2 sentences from the MLS remarks (~0.1 cent,
counted on the agent's AI allowance like every other copywrite call) and it's saved. Every later
design for that home reuses the saved lines - no new AI call. When the agent edits the lines in a
design, that edit is saved over it, so their wording carries to the next design too.
If AI isn't available (no key, allowance used up, error) the first two sentences of the remarks are
used instead - never saved, so the AI version is tried again next time.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import ListingBlurb

logger = logging.getLogger(__name__)

MAX_CHARS = 320


def blurb_key(listing_key: Optional[str], address: Optional[str], city: Optional[str]) -> Optional[str]:
    if listing_key and listing_key.strip():
        return f"mls:{listing_key.strip()}"
    street = re.sub(r"[^a-z0-9 ]", "", (address or "").lower())
    street = re.sub(r"\s+", " ", street).strip()
    if not street:
        return None
    town = re.sub(r"[^a-z0-9 ]", "", (city or "").lower()).strip()
    return f"addr:{street}|{town}"


def _first_sentences(text: str, max_chars: int = 230) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= max_chars:
        return text
    out = ""
    for sent in re.split(r"(?<=[.!?])\s+", text):
        if len(out) + len(sent) + 1 > max_chars:
            break
        out = f"{out} {sent}".strip()
    return out or text[:max_chars].rsplit(" ", 1)[0] + "..."


async def get_saved(db: AsyncSession, owner_id: str, key: str) -> Optional[ListingBlurb]:
    return (await db.execute(select(ListingBlurb).where(ListingBlurb.owner_id == owner_id, ListingBlurb.key == key))).scalar_one_or_none()


async def get_or_write(db: AsyncSession, owner_id: str, plan_tier: str, *, key: str, remarks: str, facts: str) -> dict:
    saved = await get_saved(db, owner_id, key)
    if saved:
        return {"text": saved.text, "source": "saved"}
    remarks = (remarks or "").strip()
    if not remarks and not facts:
        return {"text": "", "source": "none"}
    try:
        from app.services.ai import ai_copywrite_service
        context = "\n\n".join(p for p in [facts and f"Facts: {facts}", remarks and f"MLS remarks:\n{remarks[:2500]}"] if p)
        out = await ai_copywrite_service.generate(db, owner_id, plan_tier, purpose="design_blurb", context=context)
        text = re.sub(r"\s+", " ", (out.get("text") or "")).strip().strip('"').strip()[:MAX_CHARS]
        if text:
            db.add(ListingBlurb(owner_id=owner_id, key=key, text=text, source="ai"))
            await db.commit()
            return {"text": text, "source": "ai"}
    except Exception as e:  # noqa: BLE001 - fall back to the remarks, never block a design
        logger.info("listing blurb AI unavailable (%s) - using the remarks", getattr(e, "detail", e))
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
    return {"text": _first_sentences(remarks), "source": "remarks"}


async def save_edit(db: AsyncSession, owner_id: str, key: str, text: str) -> dict:
    text = (text or "").strip()[:MAX_CHARS * 3]
    row = await get_saved(db, owner_id, key)
    if not text:
        return {"text": row.text if row else "", "source": "saved" if row else "none"}
    if row:
        row.text, row.source = text, "agent"
    else:
        db.add(ListingBlurb(owner_id=owner_id, key=key, text=text, source="agent"))
    await db.commit()
    return {"text": text, "source": "saved"}
