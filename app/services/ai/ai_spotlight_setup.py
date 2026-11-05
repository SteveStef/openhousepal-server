"""A Spotlight made from the chat finishes its first-time setup here, on the server (owner, 11/05:
"you can leave it and it still works"): the highlights, the short story and "About the town", the
same three writes the editor makes on first open (components/property-sites/spotlight/autoSetup.ts).
It keeps going after they close the site; ai_notify posts in the chat when it's done.

The standout amenities need no write: the page picks them from the listing by itself, and the editor
ticks the same ones on open (it reads `auto_setup_server`).
"""
import asyncio
import json
import re

from app.config.logging import get_logger
from app.models.database import User
from app.services.ai import ai_notify
from app.services.ai.ai_route_bridge import RouteError, call_as_agent

logger = get_logger(__name__)


def _tidy_town(value) -> str:
    v = str(value or "").strip()
    return v if not v or re.search(r"[a-z]", v) else v.title()


def _town_context(name: str, snap: dict) -> str:
    county = re.sub(r"-[A-Za-z]{2}$", "", str(snap.get("county") or ""))
    return "\n".join(x for x in (f"Town: {name}" + (f", {snap['state']}" if snap.get("state") else ""),
                                 f"County: {_tidy_town(county)}" if county else "",
                                 f"Township: {_tidy_town(snap.get('township'))}" if snap.get("township") else "") if x)


def _town_facts(text: str):
    """The editor's parseTownFacts: keep the known fields, cut to size; None when nothing usable."""
    try:
        raw = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except ValueError:
        return None

    def s(v, n):
        return v.strip()[:n] if isinstance(v, str) else ""

    def many(v, n, top):
        return [x for x in (s(i, n) for i in (v if isinstance(v, list) else [])) if x][:top]

    dt = raw.get("downtown") if isinstance(raw.get("downtown"), dict) else {}
    around = [{"label": s(g.get("label"), 80), "detail": s(g.get("detail"), 60)} for g in (raw.get("getting_around") or []) if isinstance(g, dict)]
    facts = {"tagline": s(raw.get("tagline"), 140), "known_for": many(raw.get("known_for"), 50, 6),
             "downtown": {"name": s(dt.get("name"), 60), "where": s(dt.get("where"), 90), "highlights": many(dt.get("highlights"), 60, 4)},
             "why_move": many(raw.get("why_move"), 90, 4), "getting_around": [g for g in around if g["label"]][:3],
             "outdoors": many(raw.get("outdoors"), 60, 4), "whats_new": s(raw.get("whats_new"), 140)}
    return facts if facts["tagline"] or facts["known_for"] or facts["downtown"]["name"] or facts["why_move"] else None


async def run(user_id: str, site_id: str, story_mode: str = "short", publish: bool = False) -> None:
    from app.database import AsyncSessionLocal
    key, link = f"spotlight:{site_id}", f"/studio/property-sites/{site_id}"
    try:
        async with AsyncSessionLocal() as db:
            user = await db.get(User, user_id)
        site = await call_as_agent(user, "GET", f"/studio/property-sites/{site_id}")
        d, snap = site.get("draft_data") or {}, site.get("property_snapshot") or {}
        if d.get("auto_setup_done"):
            await ai_notify.finished(user_id, key, True, "Your Spotlight for {label} is ready.", link, "Open the Spotlight")
            return
        original = str(snap.get("description") or "").strip()
        city = _tidy_town(snap.get("city"))

        async def write(purpose: str, context: str) -> str:
            try:
                out = await call_as_agent(user, "POST", "/ai/copywrite", json={"purpose": purpose, "context": context})
                return str((out or {}).get("text") or "")
            except RouteError as e:
                logger.warning("Spotlight setup write failed", extra={"purpose": purpose, "error": str(e)})
                return ""

        wants = {}
        if original and not d.get("highlights"):
            wants["highlights"] = write("property_site_highlights", original)
        if original and story_mode != "original":
            wants["story"] = write("property_site_story_short", original)
        if city and not ((d.get("town_story") or {}).get("facts") or (d.get("town_story") or {}).get("body")):
            wants["town"] = write("property_site_town_facts", _town_context(city, snap))
        got = dict(zip(wants, await asyncio.gather(*wants.values())))

        patch: dict = {"auto_setup_done": True, "auto_setup_server": True, "story_mode": story_mode}
        missed = 0
        if "highlights" in got:
            lines = [re.sub(r"^[\s\-*•]+", "", ln).strip() for ln in got["highlights"].split("\n")]
            lines = [ln for ln in lines if ln]
            missed += not lines
            if lines:
                patch["highlights"] = lines[:8]
        if "story" in got:
            short = got["story"].strip()
            missed += not short
            if short:
                patch.update(story_short=short, description_override=short)
            else:
                patch["story_mode"] = "original"
        if "town" in got:
            facts = _town_facts(got["town"])
            missed += not facts
            if facts:
                patch["town_story"] = {"name": city, "body": "", "hidden": False, "facts": facts}
        await call_as_agent(user, "PATCH", f"/studio/property-sites/{site_id}", json=patch)
        if publish:
            await call_as_agent(user, "POST", f"/studio/property-sites/{site_id}/publish")
        done = "is live" if publish else "is ready to review"
        tail = " - a part or two needs a look in the editor." if missed else " - every section is written."
        await ai_notify.finished(user_id, key, True, f"Your Spotlight for {{label}} {done}{tail}", link, "Open the Spotlight")
    except Exception:  # noqa: BLE001
        logger.exception("Spotlight setup failed", extra={"site_id": site_id})
        await ai_notify.finished(user_id, key, False, "Your Spotlight for {label} is made, but I couldn't finish writing it. Open it and the editor finishes the rest.",
                                 link, "Open the Spotlight")
