"""The one-time setup flow (frontend /setup).

The flow never keeps its own copy of anything. Every answer is written where the features already
read it, so a change made later inside Agent Hub, Settings, Signature or the website shows up in
the flow too, and the other way round:

- About you / contact / license / bio / photo -> AgentHub.config (profile, contact,
  profilePhotoUrl) - the one place the hub, signature, vCard, website, Spotlight, reviews and
  agent emails read - mirrored onto the User columns that forms, CMA, Media and emails read
  (name, brokerage, title, market, photo_url, license_number).
- LinkedIn / office phone / office address -> AgentHub.config.contactCard (only the Save Contact
  card uses them).
- Hub look -> AgentHub.config (hubTheme, hubAccent, hubLuxeMetal, visitorDarkMode, mobileHeader).
- Everything else goes through each feature's own endpoint (Brand Kit, signature, site,
  notifications, follow-ups, calendar, CRM, reviews, slug).

users.setup_state only holds bookkeeping: the step they were on, what they skipped, and which
"looks" they confirmed (a look always has a working default, so only a confirm marks it done).
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.hub_phones import brokerage_phone, office_phone, upgrade as hub_phones_upgrade
from app.models.database import (
    AgentForm, AgentHub, AgentMarketArea, CalendarConnection, EmailSignature, MailConnection, PurchasedDomain, Site, SiteDomain,
    StudioBrandKit, User,
)
from app.utils.auth import (
    AGENT_HUB_ACCESS_TIERS, EMAIL_SIGNATURE_ACCESS_TIERS, REVIEWS_ACCESS_TIERS, tier_has_mls,
)

# Every step the flow knows, in the order it asks. Required steps can't be skipped.
STEP_ORDER = [
    "about", "contact", "license", "photo", "story", "socials", "sales", "reviews",
    "brand", "hub", "forms", "signature", "alerts", "calendar", "mailbox", "crm", "areas", "homes", "website", "domain",
]
REQUIRED = {"about", "contact", "license"}
# Steps whose answer is a choice with a working default - done once the agent confirms it here
# (or it was set inside the feature itself).
CONFIRM_STEPS = {"sales", "brand", "hub", "forms", "signature", "alerts", "homes", "website", "domain"}

# Before a plan is chosen (waiting for broker approval, or not checked out yet) the flow offers
# the head start every plan shares.
CORE_STEPS = ["about", "contact", "license", "photo", "story", "socials"]

HUB_LOOK_KEYS = {"hubTheme", "hubAccent", "hubLuxeMetal", "visitorDarkMode", "mobileHeader"}
PLACEHOLDER_PHOTO = "/home.jpg"


def steps_for_plan(plan_tier: Optional[str], approved: bool) -> list[str]:
    if not plan_tier:
        return list(CORE_STEPS)
    steps = {"about", "contact", "license", "photo", "brand", "alerts", "calendar", "mailbox", "crm"}
    if plan_tier in AGENT_HUB_ACCESS_TIERS:
        steps |= {"story", "socials", "sales", "hub", "forms"}
    if plan_tier in REVIEWS_ACCESS_TIERS:
        steps.add("reviews")
    if plan_tier in EMAIL_SIGNATURE_ACCESS_TIERS:
        steps.add("signature")
    if plan_tier == "IDX":
        steps |= {"areas", "homes", "website", "domain"}
    return [s for s in STEP_ORDER if s in steps]


def _s(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def state_of(user: User) -> dict:
    st = dict(user.setup_state or {})
    st.setdefault("skipped", [])
    st.setdefault("confirmed", [])
    st.setdefault("last_step", None)
    return st


def effective_info(user: User, cfg: dict) -> dict:
    """What every feature shows for this agent right now (hub first, account as fallback) - the
    same merge content_adapters.get_agent_profile does."""
    profile = cfg.get("profile") or {}
    contact = cfg.get("contact") or {}
    card = ((cfg.get("contactCard") or {}).get("fields") or {})
    photo = _s(cfg.get("profilePhotoUrl"))
    if photo == PLACEHOLDER_PHOTO:
        photo = ""
    return {
        "name": _s(profile.get("name")) or f"{user.first_name or ''} {user.last_name or ''}".strip(),
        "title": _s(profile.get("title")) or _s(user.professional_title),
        "brokerage": _s(profile.get("brokerage")) or _s(user.brokerage),
        "team": _s(profile.get("team")),
        "market": _s(profile.get("market")) or _s(user.market),
        "about": _s(profile.get("about")),
        "licenseNumber": _s(profile.get("licenseNumber")) or _s(contact.get("licenseNumber")) or _s(user.license_number),
        "call": _s(contact.get("call")),
        "text": _s(contact.get("text")),
        "email": _s(contact.get("email")) or _s(user.email),
        # Two numbers kept apart (hub_phones): the brokerage's main line (compliance) and the
        # agent's own office line (Save Contact).
        "brokeragePhone": brokerage_phone(cfg),
        "officePhone": office_phone(cfg),
        "officeAddress": _s((card.get("officeAddress") or {}).get("value")),
        "linkedin": _s((card.get("linkedin") or {}).get("value")),
        "photoUrl": photo or _s(user.photo_url),
        "photoPosition": cfg.get("profilePhotoPosition") or {"x": 50, "y": 50},
    }


def seed_hub_from_user(user: User, hub: AgentHub) -> bool:
    """Fill blanks in the hub config from what the agent typed at sign-up, so the public hub (which
    reads only the hub) and everything else agree from day one. Never overwrites. True if changed."""
    cfg = dict(hub.config or {})
    profile = dict(cfg.get("profile") or {})
    contact = dict(cfg.get("contact") or {})
    changed = False
    fills = {
        "name": f"{user.first_name or ''} {user.last_name or ''}".strip(),
        "title": _s(user.professional_title),
        "brokerage": _s(user.brokerage),
        "market": _s(user.market),
        "licenseNumber": _s(user.license_number),
    }
    for key, value in fills.items():
        if value and not _s(profile.get(key)):
            profile[key] = value
            changed = True
    if _s(user.email) and not _s(contact.get("email")):
        contact["email"] = user.email
        changed = True
    photo = _s(cfg.get("profilePhotoUrl"))
    if _s(user.photo_url) and (not photo or photo == PLACEHOLDER_PHOTO):
        cfg["profilePhotoUrl"] = user.photo_url
        changed = True
    if changed:
        cfg["profile"] = profile
        cfg["contact"] = contact
        hub.config = cfg
    return changed


def mirror_user_from_profile(user: User, profile: dict) -> None:
    """The User columns forms, CMA, Studio Media and emails read - kept equal to the hub profile."""
    if "name" in profile and _s(profile.get("name")):
        parts = _s(profile["name"]).split(None, 1)
        user.first_name = parts[0]
        user.last_name = parts[1] if len(parts) > 1 else ""
    if "title" in profile:
        user.professional_title = _s(profile.get("title")) or None
    if "market" in profile:
        user.market = _s(profile.get("market")) or None
    if "brokerage" in profile and _s(profile.get("brokerage")):
        user.brokerage = _s(profile["brokerage"])
    if "licenseNumber" in profile and _s(profile.get("licenseNumber")):
        user.license_number = _s(profile["licenseNumber"])


async def apply_info(db: AsyncSession, user: User, hub: AgentHub, patch: dict) -> None:
    """Merge a partial answer into the hub config (+ User mirror). License locks once set."""
    cfg = hub_phones_upgrade(dict(hub.config or {}))
    current = effective_info(user, cfg)
    if isinstance(patch.get("profile"), dict):
        profile = dict(cfg.get("profile") or {})
        incoming = {k: v for k, v in patch["profile"].items() if k in {"name", "title", "brokerage", "team", "market", "about", "licenseNumber", "brokeragePhone"} and isinstance(v, str)}
        if current["licenseNumber"]:
            incoming.pop("licenseNumber", None)  # locked once saved
        for k, v in incoming.items():
            profile[k] = v.strip() if k != "about" else v
        cfg["profile"] = profile
        mirror_user_from_profile(user, incoming)
        if "team" in incoming:
            # a team typed here also goes on the Save Contact card (its include switch follows)
            card = dict(cfg.get("contactCard") or {})
            fields = dict(card.get("fields") or {})
            team = dict(fields.get("team") or {})
            team["include"] = bool(incoming["team"].strip())
            fields["team"] = team
            card["fields"] = fields
            cfg["contactCard"] = card
    if isinstance(patch.get("contact"), dict):
        contact = dict(cfg.get("contact") or {})
        for k in ("call", "text", "email"):
            v = patch["contact"].get(k)
            if isinstance(v, str):
                contact[k] = v.strip()
        cfg["contact"] = contact
    if isinstance(patch.get("card"), dict):
        card = dict(cfg.get("contactCard") or {})
        fields = dict(card.get("fields") or {})
        for k in ("officePhone", "officeAddress", "linkedin"):
            v = patch["card"].get(k)
            if isinstance(v, str):
                f = dict(fields.get(k) or {})
                f["value"] = v.strip()
                f["include"] = bool(v.strip())
                fields[k] = f
        card["fields"] = fields
        cfg["contactCard"] = card
    if isinstance(patch.get("socialLinks"), list):
        links = []
        for item in patch["socialLinks"][:12]:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                url = _s(item.get("url"))
                links.append({"id": item["id"], "label": _s(item.get("label")) or item["id"], "url": url, "enabled": bool(url) and item.get("enabled", True) is not False})
        cfg["socialLinks"] = links
    if isinstance(patch.get("photoPosition"), dict):
        pos = patch["photoPosition"]
        try:
            cfg["profilePhotoPosition"] = {"x": max(0, min(100, float(pos.get("x", 50)))), "y": max(0, min(100, float(pos.get("y", 50))))}
        except (TypeError, ValueError):
            pass
    if isinstance(patch.get("look"), dict):
        for k, v in patch["look"].items():
            if k in HUB_LOOK_KEYS and isinstance(v, (str, bool)):
                cfg[k] = v
    hub.config = cfg


async def compute(db: AsyncSession, user: User, hub: AgentHub) -> dict:
    """Steps for this agent's plan with live status: done / skipped / todo."""
    cfg = hub.config or {}
    info = effective_info(user, cfg)
    st = state_of(user)
    approved = bool(user.broker_authorized) or user.is_admin
    plan = user.plan_tier if approved else None
    steps = steps_for_plan(plan, approved)
    confirmed = set(st["confirmed"])

    async def _exists(q) -> bool:
        return bool((await db.execute(q)).scalar())

    done: dict[str, bool] = {}
    for step in steps:
        if step == "about":
            done[step] = all(info[k] for k in ("name", "title", "brokerage", "market"))
        elif step == "contact":
            done[step] = all(info[k] for k in ("call", "text", "email", "brokeragePhone"))
        elif step == "license":
            done[step] = bool(info["licenseNumber"])
        elif step == "photo":
            done[step] = bool(info["photoUrl"])
        elif step == "story":
            done[step] = bool(info["about"])
        elif step == "socials":
            done[step] = bool(info["linkedin"]) or any(isinstance(l, dict) and l.get("enabled") and _s(l.get("url")) for l in (cfg.get("socialLinks") or []))
        elif step == "sales":
            done[step] = "sales" in confirmed or (not tier_has_mls(plan) and bool(cfg.get("recentSales")))
        elif step == "reviews":
            r = cfg.get("reviews") or {}
            done[step] = bool(_s(r.get("google_review_url")) or _s(r.get("zillow_review_url")) or _s(r.get("google_place_id")))
        elif step == "brand":
            kit = (await db.execute(select(StudioBrandKit).where(StudioBrandKit.owner_id == user.id))).scalar_one_or_none()
            done[step] = "brand" in confirmed or bool(kit and (kit.personal_logo_url or kit.team_logo_url or kit.brokerage_logo_url))
        elif step == "hub":
            done[step] = "hub" in confirmed or "hubTheme" in cfg
        elif step == "forms":
            # Forms match the hub out of the box - done once confirmed, or a form got its own look.
            done[step] = "forms" in confirmed or await _exists(
                select(AgentForm.id).where(AgentForm.owner_id == user.id, AgentForm.look_mode == "custom").limit(1)
            )
        elif step == "signature":
            sig = (await db.execute(select(EmailSignature).where(EmailSignature.owner_id == user.id))).scalar_one_or_none()
            done[step] = "signature" in confirmed or bool(sig and (sig.config or {}).get("installedHtmlHash"))
        elif step == "alerts":
            done[step] = "alerts" in confirmed
        elif step == "calendar":
            done[step] = await _exists(select(func.count()).select_from(CalendarConnection).where(CalendarConnection.owner_id == user.id))
        elif step == "mailbox":
            mb = (await db.execute(select(MailConnection).where(MailConnection.owner_id == user.id))).scalar_one_or_none()
            done[step] = bool(mb and mb.enabled and mb.status == "ok")
        elif step == "crm":
            done[step] = bool(user.crm_push_enabled and (user.crm_fub_api_key or user.crm_webhook_url or user.crm_parse_email))
        elif step == "areas":
            done[step] = await _exists(select(func.count()).select_from(AgentMarketArea).where(AgentMarketArea.owner_id == user.id))
        elif step == "homes":
            # Which homes the website shows (header + Featured page) - a choice with a working default.
            done[step] = "homes" in confirmed
        elif step == "website":
            site = (await db.execute(select(Site).where(Site.owner_id == user.id))).scalar_one_or_none()
            done[step] = "website" in confirmed or bool(site and (site.status == "PUBLISHED" or site.is_enabled is False))
        elif step == "domain":
            # The web address: keeping the free one (confirmed), connecting their own, or buying one.
            done[step] = "domain" in confirmed or await _exists(
                select(SiteDomain.id).where(SiteDomain.owner_id == user.id).limit(1)
            ) or await _exists(
                select(PurchasedDomain.id).where(PurchasedDomain.owner_id == user.id, PurchasedDomain.status.in_(["REGISTERING", "ACTIVE"])).limit(1)
            )

    skipped = set(st["skipped"])
    items = []
    for step in steps:
        status = "done" if done[step] else ("skipped" if step in skipped and step not in REQUIRED else "todo")
        items.append({"id": step, "status": status, "required": step in REQUIRED})
    remaining = [i for i in items if i["status"] != "done"]
    return {
        "plan": plan,
        "approved": approved,
        "has_mls": tier_has_mls(plan) if plan else bool(user.mls_id),
        "steps": items,
        "last_step": st["last_step"] if st["last_step"] in steps else None,
        "seen": st.get("seen") or [],
        # finished = nothing left to ask (skipped ones still show on the dashboard)
        "finished": all(i["status"] != "todo" for i in items),
        "complete": not remaining,
        "required_missing": [i["id"] for i in items if i["required"] and i["status"] != "done"],
        "info": info,
        "slug": hub.slug,
        "social_links": cfg.get("socialLinks") or [],
        "look": {k: cfg.get(k) for k in HUB_LOOK_KEYS if k in cfg},
        "reviews": cfg.get("reviews") or {},
    }
