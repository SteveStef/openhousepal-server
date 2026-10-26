"""Fair Housing wording check for neighborhood (town) text - automatic, nothing for the agent to do.

Every piece of written text a town page shows (the town write-up, each category's summary, each
place's one-line description, the market-history notes) goes through two layers:

  1. clean_text() - fixed rules, free, instant. Applied when text is stored AND again every time a
     town page is served, so nothing slips through even if a rule is added later. Rewords only:
     "family-friendly" -> "welcoming", drops "safe" from "safe neighborhood", "master bedroom" ->
     "primary bedroom", "top-rated schools" -> "schools", and so on.
  2. ai_fix_text() - the same Fair Housing reviewer the AI copywriter uses (ai_copywrite_service),
     run when text is created or refreshed (never per page view). It returns risky phrases with a
     compliant replacement; a replacement is applied only if it keeps every number and every
     proper name of the original phrase (so a fix can never change a material fact - a price, a
     school, a place, a distance), otherwise the phrase is left to the fixed rules.

What we never put on a town page at all (by design, not by filter): crime figures, race/ethnicity
or religion data, "who lives here" descriptions, school ratings or rankings.
"""
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.database import MarketArea

logger = logging.getLogger(__name__)

# (pattern, replacement). Patterns use (?i:...) where case shouldn't matter; school-name
# lookaheads stay case-sensitive so they only fire before a real school / district name.
_ADJ_SCHOOL = r"(?i:award[- ]winning|top[- ]rated|highly[- ]rated|top[- ]ranked|nationally[- ]ranked|blue[- ]ribbon|acclaimed|excellent|exceptional|outstanding|great|good|top|best|sought[- ]after|desirable|well[- ]regarded|highly[- ]regarded)"
_RULES: list[tuple[str, str]] = [
    # familial status / age (the whole "perfect for ... alike" phrase first)
    (r"(?i:\bfor (?:young |growing |new )?(?:families|kids|children|empty[- ]nesters|retirees|seniors|couples|young professionals|singles)(?:,? (?:and|or) (?:young |growing |new )?(?:families|kids|children|empty[- ]nesters|retirees|seniors|couples|young professionals|singles))*(?: alike)?)", "for buyers"),
    (r"(?i:(?:a |an )?(?:safe|secure|great|wonderful|perfect|ideal) (?:place|spot|area|town|community) to (?:raise|bring up) (?:a family|families|kids|children))", "a place many people choose to live"),
    (r"(?i:(?:very |super |especially |extremely )?(?:family|kid|child)[- ]friendly)", "welcoming"),
    (r"(?i:(?:young|growing|new) families)", "buyers"),
    (r"(?i:families with (?:young |school[- ]aged )?(?:kids|children))", "buyers"),
    (r"(?i:\b(?:empty[- ]nesters|retirees|young professionals|young couples|newlyweds|singles|bachelors)\b)", "buyers"),
    (r"(?i:\badults?[- ]only\b)", ""),
    # safety / crime implications
    (r"(?i:\b(?:very |extremely )?safe(?:,| and)? (?=(?:quiet |friendly |walkable |tree-lined |leafy )?(?:neighborhoods?|communit(?:y|ies)|areas?|places?|streets?|towns?|suburbs?|environment|setting)\b))", ""),
    (r"(?i:\b(?:low|lower)[- ]crime )", ""),
    (r"(?i:\bcrime[- ]free )", ""),
    # exclusion / steering
    (r"(?i:\bexclusive\b)", "distinctive"),
    (r"(?i:\bexclusivity\b)", "distinction"),
    (r"(?i:\b(?:integrated|segregated|ethnic) (?=neighborhood|community|area))", ""),
    # disability
    (r"(?i:\bhandicap(?:ped)?[- ]accessible\b)", "accessible"),
    (r"(?i:\bhandicap(?:ped)?\b)", "accessible"),
    # school quality claims - we name districts and schools, never rate them
    (_ADJ_SCHOOL + r" (?=(?:the )?(?:[A-Z][\w'.-]* ){0,5}(?:School|Schools|Academy|High|Elementary|Middle)\b|(?i:public )?(?i:schools?|school districts?)\b)", ""),
    # dated terms
    (r"(?i:\bmaster (?=bed(?:room)?s?\b|suites?\b|bath(?:room)?s?\b))", "primary "),
]
_COMPILED = [(re.compile(p), r) for p, r in _RULES]


def _keep_case(original: str, replacement: str) -> str:
    if replacement and original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def clean_text(text: Optional[str]) -> str:
    """Fixed-rule rewording. Safe to call on anything, any number of times."""
    if not text or not isinstance(text, str):
        return text or ""
    out = text
    for rx, rep in _COMPILED:
        out = rx.sub(lambda m: _keep_case(m.group(0), rep), out)
    # tidy what a removal can leave behind
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    out = re.sub(r",\s*(?=(?:neighborhoods?|communit(?:y|ies)|areas?|streets?|towns?|suburbs?)\b)", " ", out)
    out = re.sub(r"(^|[.!?]\s+)[,;:\s]+", r"\1", out)
    out = re.sub(r"(?i)\b(a|an) (?=\w)", lambda m: m.group(0), out)
    out = re.sub(r"\b([Aa])n? (?=([A-Za-z]))", lambda m: (m.group(1) + ("n " if m.group(2).lower() in "aeiou" else " ")), out)
    out = re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), out)
    return out.strip()


def population_area_label(geography: str) -> Optional[str]:
    """What the Census population number actually covers, in words: "Malvern borough", "Wayne
    (Census area)", "ZIP 19035". The town page covers the whole mailing-address area, which can be far
    bigger than the borough or Census area of the same name - the label says which. None for an old
    record that averaged several ZIPs' head counts (wrong - fixed in census_service)."""
    if geography.startswith("ZCTA average") and "," in geography:
        return None
    m = re.search(r" - (.+?), [A-Za-z ]+$", geography)
    if m:
        name = m.group(1)
        return re.sub(r"\s+CDP$", " census area", name)
    m = re.search(r"\((\d{5})\)", geography)
    if m:
        return f"ZIP {m.group(1)}"
    return geography or None  # "ZIP 19035" / "ZIPs 19341, 19355" (census_service)


def clean_area_payload(facts: dict, narrative: Optional[dict]) -> tuple[dict, Optional[dict]]:
    """Render-time pass over everything a town page prints (see public_site_routes)."""
    from app.services.areas.town_places_check import medical_label, tidy_research_text
    facts = dict(facts or {})
    checked = bool(facts.get("_places_check_version"))
    # Outside sites' price figures (Movoto, Redfin, blogs) disagreed with each other and with the live
    # MLS numbers on the same page (Malvern: "up 2.5%" and "down 2%") - the page shows the MLS only.
    facts.pop("market_context", None)
    for key, block in list(facts.items()):
        if not isinstance(block, dict):
            continue
        block = dict(block)
        block.pop("_removed", None)
        if isinstance(block.get("notes"), str):
            block["notes"] = tidy_research_text(clean_text(block["notes"]))
        if isinstance(block.get("entities"), list):
            ents = []
            for e in block["entities"]:
                if not isinstance(e, dict):
                    ents.append(e)
                    continue
                e = dict(e)
                if e.get("description"):
                    e["description"] = tidy_research_text(clean_text(e["description"]))
                if key == "medical":
                    e["category"] = medical_label(e.get("category"))
                if not checked or not e.get("google_checked") and not e.get("survey_matched"):
                    e.pop("distance_miles", None)  # the research's own guess ("0 mi") - never shown
                ents.append(e)
            block["entities"] = ents
        facts[key] = block
    # Only the town's population leaves the server - age, income, household make-up and similar
    # resident descriptions are never shown on a home-sale site (Fair Housing).
    demo = facts.get("demographics")
    if isinstance(demo, dict):
        demo = {k: v for k, v in demo.items() if k in ("population", "source", "fetched_at", "geography", "year", "vintage")}
        pop = demo.get("population")
        if isinstance(pop, dict):
            label = population_area_label(str(pop.get("geography") or ""))
            if label is None:
                demo.pop("population")  # an old multi-ZIP average - a head count can't be averaged
            else:
                demo["population"] = {**pop, "label": label}
        facts["demographics"] = demo
    _one_school_district_share(facts)
    if narrative and isinstance(narrative.get("summary"), str):
        narrative = {**narrative, "summary": tidy_research_text(clean_text(narrative["summary"]))}
    return facts, narrative


def _one_school_district_share(facts: dict) -> None:
    """The Schools list and the town map said different shares for the same district (Wayne: Radnor
    48% vs 45%) - the list counted MLS school fields, the map Census borders. The page shows one
    number: the map's (every home's location against the real district border)."""
    from app.services.areas.boundary_service import district_key
    mapped = {district_key(d.get("name") or ""): d.get("share") for d in ((facts.get("boundaries") or {}).get("school_districts") or [])
              if isinstance(d, dict) and d.get("share") is not None}
    ps = facts.get("public_schools")
    if not mapped or not isinstance(ps, dict) or not isinstance(ps.get("districts"), list):
        return
    districts = []
    for d in ps["districts"]:
        share = mapped.get(district_key(d.get("name") or "")) if isinstance(d, dict) else None
        districts.append({**d, "share": share} if share is not None else d)
    out = {**ps, "districts": districts}
    caveat = ps.get("boundary_caveat")
    if isinstance(caveat, str):  # the sentence under the list quotes the same shares
        for d in districts:
            if isinstance(d, dict) and d.get("name") and d.get("share") is not None:
                caveat = re.sub(rf"({re.escape(d['name'])} \()\d+(% of homes\))", rf"\g<1>{d['share']}\g<2>", caveat)
        out["boundary_caveat"] = caveat
    facts["public_schools"] = out


# ---------------------------------------------------------------- AI pass (store time only)

_NUM = re.compile(r"\d[\d,.]*")
_PROPER = re.compile(r"\b[A-Z][a-zA-Z'&.-]+(?:\s+[A-Z][a-zA-Z'&.-]+)*")


def _keeps_facts(phrase: str, replacement: str) -> bool:
    """A fix may only reword: every number and every capitalized name in the phrase must still be
    in the replacement (sentence-initial capitals are ignored)."""
    nums = set(_NUM.findall(phrase))
    if not nums.issubset(set(_NUM.findall(replacement))):
        return False
    words = phrase.split()
    names = {m for m in _PROPER.findall(" ".join(words[1:]))} if words else set()
    return all(n in replacement for n in names)


async def ai_fix_text(db: AsyncSession, text: str, triggering_user_id: Optional[str]) -> str:
    text = clean_text(text)
    if not text or len(text) < 25:
        return text
    from app.services.ai.ai_config_service import ai_config_service
    from app.services.ai.ai_copywrite_service import _COMPLIANCE_SYSTEM, _COMPLIANCE_TOOL
    from app.services.ai.ai_orchestrator_service import _get_client
    from app.services.ai.ai_rate_limit_service import AIRateLimitExceeded, reconcile_budget, record_usage_event, reserve_global_and_emergency

    config = await ai_config_service.get_config(db)
    try:
        # Shared town text is checked once for every agent - charged to the same platform-wide
        # bucket as the town write-up itself, never to one agent.
        reservation = await reserve_global_and_emergency(
            db, "area_narrative_global", config["area_narrative_global_reservation_estimate_usd"],
            None, config["area_narrative_monthly_cost_ceiling_usd"], config,
        )
    except AIRateLimitExceeded:
        return text
    model = config.get("copywrite_model") or config["model"]
    try:
        resp = await _get_client().messages.create(
            model=model, max_tokens=800, system=_COMPLIANCE_SYSTEM + (
                " This is a neighborhood description on a real estate website. Do not flag place names, "
                "business names, or plain facts (distances, prices, counts, school or district names). Do not "
                "flag 'walking', 'walk to', 'walkable', 'walking trails' or 'walking distance' - HUD treats "
                "describing a place's walkability as acceptable."),
            tools=[_COMPLIANCE_TOOL], tool_choice={"type": "tool", "name": "report_flags"},
            messages=[{"role": "user", "content": f"Review this real estate copy:\n\n{text}"}],
        )
        cost = ai_config_service.estimate_cost_usd(config, model, resp.usage.input_tokens, resp.usage.output_tokens)
        await record_usage_event(db, triggering_user_id, None, model, resp.usage.input_tokens, resp.usage.output_tokens,
                                 cost, 0, False, None, event_type="area_fair_housing")
        await reconcile_budget(db, reservation, cost)
    except Exception:
        logger.exception("fair housing AI check failed")
        await reconcile_budget(db, reservation, 0.0)
        return text
    for b in resp.content:
        if getattr(b, "type", None) == "tool_use" and b.name == "report_flags":
            for f in (b.input.get("flags") or [])[:20]:
                phrase, sug = str((f or {}).get("phrase") or ""), str((f or {}).get("suggestion") or "")
                if phrase and phrase in text and _keeps_facts(phrase, sug):
                    text = text.replace(phrase, sug, 1)
    return clean_text(text)


async def fix_area_text(db: AsyncSession, area: MarketArea, triggering_user_id: Optional[str] = None, use_ai: bool = True) -> int:
    """Store-time pass over a town's written text (shared by every agent). The first time a piece
    of text is changed its original is kept under facts['_fair_housing']['originals']. Returns how
    many pieces changed."""
    facts = dict(area.facts or {})
    log = dict(facts.get("_fair_housing") or {})
    originals = dict(log.get("originals") or {})
    changed = 0

    async def fix(key: str, value: str) -> str:
        nonlocal changed
        new = await ai_fix_text(db, value, triggering_user_id) if use_ai else clean_text(value)
        if new != value:
            originals.setdefault(key, value)
            changed += 1
        return new

    for cat, block in list(facts.items()):
        if cat.startswith("_") or not isinstance(block, dict):
            continue
        block = dict(block)
        if isinstance(block.get("notes"), str) and block["notes"].strip():
            block["notes"] = await fix(f"{cat}.notes", block["notes"])
        if isinstance(block.get("entities"), list):
            ents = []
            for i, e in enumerate(block["entities"]):
                if isinstance(e, dict) and isinstance(e.get("description"), str) and e["description"].strip():
                    # Place one-liners are short and factual - fixed rules are enough (no AI call each).
                    new = clean_text(e["description"])
                    if new != e["description"]:
                        originals.setdefault(f"{cat}.{i}.description", e["description"])
                        changed += 1
                    e = {**e, "description": new}
                ents.append(e)
            block["entities"] = ents
        facts[cat] = block
    narrative = dict(area.narrative or {})
    if isinstance(narrative.get("summary"), str) and narrative["summary"].strip():
        narrative["summary"] = await fix("narrative.summary", narrative["summary"])
        area.narrative = narrative
        flag_modified(area, "narrative")
    log.update({"checked_at": datetime.now(timezone.utc).isoformat(), "originals": originals})
    facts["_fair_housing"] = log
    area.facts = facts
    flag_modified(area, "facts")
    db.add(area)
    await db.commit()
    return changed
