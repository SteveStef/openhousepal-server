"""The Brand Kit's LOOK - the one central look an agent sets once in Studio -> Brand Kit (or the
setup flow's Brand step) that every feature wears by default:

    theme   'classic' | 'luxe'          (Luxe is IDX only - other plans fall back to Classic)
    accent  the Brand Kit's secondary_color (the Classic color - same palette as Hub / IDX)
    metal   'gold' | 'silver' | 'bronze'  (Luxe's finish)
    dark    False = Ivory, True = Midnight

Stored WITHOUT a migration: theme / metal / dark live in users.setup_state["brand_look"] (that
JSONB already exists and setup_routes keeps unknown keys), the accent is the existing
StudioBrandKit.secondary_color. Mirrors frontend lib/brandKit.ts.

Which features follow it ("My Brand Kit") and how:
  * Agent Hub     config.lookSource 'brand' | 'own'. No lookSource = legacy: a hub that never saved
                  a look (no hubTheme/hubAccent/hubLuxeMetal, not dark) follows the brand; one that
                  did keeps its own. Resolved at READ time (public page + editor GET), never stored.
  * Forms         agent_forms.look_mode 'brand' (new default) | 'custom' | 'hub' (legacy: wears the
                  hub's resolved look, so nothing an agent already sees changes).
  * Signature     config.lookSource 'brand' | 'own'. An empty (never saved) signature follows the
                  brand; a saved one keeps its own. Materialized: the brand values are written into
                  the config (GET /me, PUT /me, and every Brand Kit save) because ~20 image
                  endpoints read signature.config directly.
  * IDX website   SiteTheme.follow_brand_kit (already existed for colors / fonts / logo). The look
                  (template / mode / metal) only follows once the agent has SAVED a brand look, so
                  every existing site keeps its template until then.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import EmailSignature, StudioBrandKit, User

HUB_DEFAULT_ACCENT = "#C9A24D"  # Agent Hub Classic "Original" - frontend HUB_CLASSIC_DEFAULT_ACCENT
METALS = ("gold", "silver", "bronze")
_HEX6 = re.compile(r"^#[0-9a-fA-F]{6}$")
_HEX3 = re.compile(r"^#[0-9a-fA-F]{3}$")

# Mirrors frontend lib/luxeMetals.ts base tones.
METAL_BASE = {"gold": "#C9A24D", "silver": "#A9AFB7", "bronze": "#A9714B"}


def _hex6(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    v = value.strip()
    if _HEX3.match(v):
        v = "#" + "".join(c * 2 for c in v[1:])
    return v.upper() if _HEX6.match(v) else None


def _lum(hex6: str) -> float:
    n = int(hex6[1:], 16)
    r, g, b = (n >> 16) & 255, (n >> 8) & 255, n & 255
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


def classic_accent(value: Any) -> str:
    """A Brand Kit color as a Classic accent: any real color except near-white (it would vanish on
    the buttons) - those fall back to the hub's Original gold. Same rule as brandClassicAccent in
    frontend lib/brandKit.ts."""
    h = _hex6(value)
    return h if h and _lum(h) <= 0.85 else HUB_DEFAULT_ACCENT


def stored_look(user: User) -> Optional[dict]:
    raw = (user.setup_state or {}).get("brand_look") if isinstance(user.setup_state, dict) else None
    return raw if isinstance(raw, dict) else None


def look_of(user: User, kit: Optional[StudioBrandKit]) -> dict:
    """The agent's brand look, always complete. `saved` = they picked one (not just defaults)."""
    raw = stored_look(user) or {}
    from app.utils.auth import luxe_theme_allowed
    theme = "luxe" if raw.get("theme") == "luxe" else "classic"
    return {
        "theme": theme,
        # what features actually show - Luxe is IDX only
        "shown_theme": theme if theme != "luxe" or luxe_theme_allowed(user) else "classic",
        "metal": raw.get("metal") if raw.get("metal") in METALS else "gold",
        "dark": raw.get("dark") is True,
        "accent": classic_accent(kit.secondary_color if kit else None),
        "primary": (kit.primary_color if kit else None) or "#111827",
        "saved": bool(raw),
    }


async def get_kit(db: AsyncSession, owner_id: str) -> Optional[StudioBrandKit]:
    return (await db.execute(select(StudioBrandKit).where(StudioBrandKit.owner_id == owner_id))).scalar_one_or_none()


async def get_look(db: AsyncSession, user: User) -> dict:
    return look_of(user, await get_kit(db, user.id))


def save_look(user: User, patch: dict) -> dict:
    """Merge a look patch into users.setup_state["brand_look"] (caller commits)."""
    st = dict(user.setup_state or {})
    cur = dict(st.get("brand_look") or {})
    if patch.get("theme") in ("classic", "luxe"):
        cur["theme"] = patch["theme"]
    if patch.get("metal") in METALS:
        cur["metal"] = patch["metal"]
    if isinstance(patch.get("dark"), bool):
        cur["dark"] = patch["dark"]
    cur.setdefault("theme", "classic")
    cur.setdefault("metal", "gold")
    cur.setdefault("dark", False)
    st["brand_look"] = cur
    user.setup_state = st
    return cur


# ---------- Agent Hub ----------

_HUB_OWN_KEYS = ("hubTheme", "hubAccent", "hubLuxeMetal")


def hub_follows_brand(cfg: Optional[dict]) -> bool:
    cfg = cfg or {}
    src = cfg.get("lookSource")
    if src in ("brand", "own"):
        return src == "brand"
    return not any(k in cfg for k in _HUB_OWN_KEYS) and cfg.get("visitorDarkMode") is not True


def hub_patch(look: dict) -> dict:
    return {
        "hubTheme": look["theme"],
        "hubAccent": look["accent"],
        "hubLuxeMetal": look["metal"],
        "visitorDarkMode": look["dark"],
        "lookSource": "brand",
    }


def hub_config_with_brand(cfg: Optional[dict], look: dict) -> dict:
    """The hub config as visitors / the editor see it: the brand look laid over it while the hub
    follows the brand (read-time only - nothing is written)."""
    cfg = dict(cfg or {})
    return {**cfg, **hub_patch(look)} if hub_follows_brand(cfg) else cfg


# ---------- Email Signature ----------

_SIG_LOOK_KEYS = ("accentColor", "luxe", "luxeMetal", "darkMode")


def signature_follows_brand(cfg: Optional[dict]) -> bool:
    cfg = cfg or {}
    src = cfg.get("lookSource")
    if src in ("brand", "own"):
        return src == "brand"
    return not any(k in cfg for k in _SIG_LOOK_KEYS)


def signature_patch(look: dict, user: User) -> dict:
    from app.utils.auth import luxe_theme_allowed
    luxe = look["theme"] == "luxe" and luxe_theme_allowed(user)
    return {
        "lookSource": "brand",
        "luxe": luxe,
        "luxeMetal": look["metal"],
        "accentColor": METAL_BASE[look["metal"]] if luxe else look["accent"],
        "darkMode": look["dark"],
    }


def signature_config_with_brand(cfg: Optional[dict], look: dict, user: User) -> dict:
    cfg = dict(cfg or {})
    return {**cfg, **signature_patch(look, user)} if signature_follows_brand(cfg) else cfg


# ---------- Forms ----------

def form_brand_look(look: dict) -> dict:
    return {"theme": look["shown_theme"], "accent": look["accent"], "metal": look["metal"], "dark": look["dark"]}


# ---------- push on Brand Kit save ----------

async def propagate(db: AsyncSession, user: User) -> None:
    """After a Brand Kit save: write the brand look into the signature if it follows the brand
    (the hub, forms and IDX resolve it on read). Caller commits."""
    look = await get_look(db, user)
    sig = (await db.execute(select(EmailSignature).where(EmailSignature.owner_id == user.id))).scalar_one_or_none()
    if sig is not None and signature_follows_brand(sig.config):
        nxt = signature_config_with_brand(sig.config, look, user)
        if nxt != (sig.config or {}):
            sig.config = nxt
