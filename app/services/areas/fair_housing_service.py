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


def clean_area_payload(facts: dict, narrative: Optional[dict], origin: Optional[tuple[float, float]] = None,
                       town: Optional[str] = None) -> tuple[dict, Optional[dict]]:
    """Render-time pass over everything a town page prints (see public_site_routes). `origin` is the
    town center the checked distances were measured from; `town` the page's own town name."""
    from app.services import nearby_amenities_service as nas
    from app.services.areas.town_places_check import drop_removed_mentions, fix_district_names, medical_label, tidy_research_text
    facts = dict(facts or {})
    checked = bool(facts.get("_places_check_version"))
    districts = town_district_names(facts)
    kept = {str(e.get("name") or "").lower() for b in facts.values() if isinstance(b, dict)
            for e in (b.get("entities") or []) if isinstance(e, dict)}
    removed = [str(r.get("name")) for b in facts.values() if isinstance(b, dict)
               for r in (b.get("_removed") or []) if isinstance(r, dict) and r.get("name") and str(r["name"]).lower() not in kept]
    # Checked places whose own address is in another town (Chester County Hospital, West Chester).
    elsewhere = [str(e.get("name")) for b in facts.values() if isinstance(b, dict) for e in (b.get("entities") or [])
                 if isinstance(e, dict) and e.get("name") and e.get("google_checked") and e.get("city") and town
                 and str(e["city"]).strip().lower() != town.strip().lower()]
    # Outside sites' price figures (Movoto, Redfin, blogs) disagreed with each other and with the live
    # MLS numbers on the same page (Malvern: "up 2.5%" and "down 2%") - the page shows the MLS only.
    facts.pop("market_context", None)
    for key, block in list(facts.items()):
        if not isinstance(block, dict):
            continue
        block = dict(block)
        block.pop("_removed", None)
        if isinstance(block.get("notes"), str):
            block["notes"] = fix_district_names(drop_removed_mentions(tidy_research_text(clean_text(block["notes"])), removed), districts)
            block["notes"] = _drop_wrong_place_claims(block["notes"], town, elsewhere)
        if isinstance(block.get("entities"), list):
            ents = []
            for e in block["entities"]:
                if not isinstance(e, dict):
                    ents.append(e)
                    continue
                e = dict(e)
                if key == "transportation" and (_BUS_ROUTE_NAME_RE.search(str(e.get("name") or ""))
                                                or re.search(r"\btours?\b", f"{e.get('name')} {e.get('category')}", re.IGNORECASE)):
                    continue  # a numbered bus route (SEPTA Route 106 ended) or a sightseeing tour (Gettysburg Tour Center)
                if key == "parks" and (e.get("distance_miles") or 0) > FAR_PARK_MILES \
                        and not re.search(r"\b(?:state|national)\s+(?:park|forest|seashore|recreation area)\b", str(e.get("name") or ""), re.IGNORECASE):
                    continue  # not a local park (Bryn Coed Preserve, 22 mi from Bryn Mawr; Glenolden's sports complex for Radnor)
                if key == "parks" and e.get("name"):
                    e["name"] = re.sub(r"\s+(?:Park\s+)?(?:Office|Headquarters|Visitor Center)$", "", str(e["name"])) or e["name"]  # "White Clay Creek Preserve Office"
                if key == "medical" and e.get("description") and "urgent" in str(e.get("category") or "").lower():
                    e["description"] = re.sub(r"\bemergency care\b", "walk-in care", str(e["description"]), flags=re.IGNORECASE)  # urgent care isn't an ER
                if key == "private_schools" and e.get("description") and _grades_disagree(e.get("category"), e["description"]):
                    e.pop("description")  # "Grades K-11" over "Independent K-8 school" (Main Line Classical Academy)
                if e.get("description"):
                    e["description"] = tidy_research_text(clean_text(e["description"]))
                    e["description"] = _not_in_another_town(e["description"], town, e.get("city"))
                if key == "parks":
                    e["category"] = _plain_park_label(e.get("category"), e.get("name"))
                if key == "medical":
                    e["category"] = medical_label(e.get("category"))
                if not checked or not e.get("google_checked") and not e.get("survey_matched"):
                    e.pop("distance_miles", None)  # the research's own guess ("0 mi") - never shown
                elif origin and e.get("lat") is not None and e.get("lng") is not None and e.get("distance_miles") is not None:
                    # Inside a mile, one-way downtown streets made driving miles jumble the order
                    # (Rehoboth: Penny Lane 1.1 mi by car, Cafe Azafran 0.6 - both a few blocks off);
                    # the straight-line distance from the same town center reads true there.
                    straight = nas._haversine_miles(origin[0], origin[1], e["lat"], e["lng"])
                    if straight < 1:
                        e["distance_miles"] = max(0.1, round(straight, 1))  # never "0 mi" for a place down the street
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
        narrative = {**narrative, "summary": fix_district_names(tidy_research_text(clean_text(narrative["summary"])), districts)}
    return facts, narrative


def town_display(area, facts: Optional[dict] = None, narrative: Optional[dict] = None) -> tuple[dict, Optional[dict]]:
    """A town's facts and summary exactly as visitors may see them - the one step every place that
    shows a town uses (IDX town page, compare page, neighborhood cards, blog town cards, admin
    preview), so a fix to what the page says reaches all of them. Free: rules only, no lookups."""
    from app.services.areas.school_display import tidy_school_facts
    from app.services.areas.town_extras_service import commute_for_page
    lat, lng = getattr(area, "centroid_lat", None), getattr(area, "centroid_lng", None)
    center = (lat, lng) if lat is not None and lng is not None else None
    facts = tidy_school_facts(dict(getattr(area, "facts", None) or {}) if facts is None else facts, center)
    facts, narrative = clean_area_payload(facts, getattr(area, "narrative", None) if narrative is None else narrative, center, getattr(area, "name", None))
    facts.pop("_fair_housing", None)
    if isinstance(facts.get("counties"), list) and facts["counties"]:
        from app.services.areas.town_map_service import county_label
        facts["county_label"] = county_label(facts["counties"]) or facts.get("county_label")  # today's wording, not the stored one
    if facts.get("commute"):  # no "Princeton -> Princeton" trip (town_extras_service.is_self_destination)
        facts["commute"] = commute_for_page(facts["commute"], getattr(area, "name", None), lat, lng)
    return facts, narrative


FAR_PARK_MILES = 15
_GRADE_SPAN_RE = re.compile(r"\b(Pre-?K|PK|K|N|\d{1,2})\s*(?:-|–|through|to)\s*(\d{1,2})\b", re.IGNORECASE)


def _grades_disagree(category, description: str) -> bool:
    """The card's checked grades ("Grades K-11", from the federal survey) against a grade span the
    research wrote into the description ("Independent K-8 school")."""
    shown = _GRADE_SPAN_RE.search(str(category or "")) if str(category or "").lower().startswith("grades") else None
    said = _GRADE_SPAN_RE.search(description or "")
    if not shown or not said:
        return False
    low = lambda g: "0" if g.lower() in ("k", "pk", "pre-k", "prek", "n") else g  # noqa: E731
    return (low(shown.group(1)), shown.group(2)) != (low(said.group(1)), said.group(2))


_BUS_ROUTE_NAME_RE = re.compile(r"\broutes?\s*\d+|\bbus route\b", re.IGNORECASE)


def _drop_wrong_place_claims(notes: str, town: Optional[str], elsewhere: list[str]) -> str:
    """"Landenberg has direct access to Chester County Hospital within the community" - but its checked
    address is West Chester, 22 miles away. A sentence that puts such a place inside the town goes."""
    if not notes or not town or not elsewhere:
        return notes
    inside = (rf"(?:\bwithin (?:the )?(?:community|town|borough|city|village)\b|\b{re.escape(town)}\s+proper\b|"
              rf"\b(?:located|based) in {re.escape(town)}\b|\bin {re.escape(town)} itself\b)")
    # The claim has to be about that place: its name, then the "inside" words shortly after, with no
    # "nearby / adjacent / plus" between (Villanova: "Willows Park ... within town, plus nearby Radnor Trail").
    gap = r"(?:(?!\b(?:nearby|adjacent|neighbou?ring|plus|outside)\b)[^.]){0,60}?"
    claims = [re.compile(r"\b" + re.escape(n) + r"\b" + gap + inside, re.IGNORECASE) for n in elsewhere if len(n) >= 5]
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", notes) if s.strip()]
    keep = [s for s in sentences if not any(p.search(s) for p in claims)]
    return notes if len(keep) == len(sentences) else " ".join(keep).strip()


def _not_in_another_town(description: str, town: Optional[str], city: Optional[str]) -> str:
    """"Coffee roaster in Gladwyne" under a listing whose address is Haverford: the town part goes."""
    if not description or not town or not city or str(city).strip().lower() == town.strip().lower():
        return description
    out = re.sub(rf",?\s*\b(?:located\s+)?in\s+(?:downtown\s+|central\s+)?{re.escape(town)}\b(?:\s+proper)?", "", description, flags=re.IGNORECASE)
    out = re.sub(r"\s+([,.;])", r"\1", out).strip()
    return out[:1].upper() + out[1:] if out else out


def _plain_park_label(category: Optional[str], name: Optional[str]) -> Optional[str]:
    """"Regional park" for a township park (Ashbridge Memorial Park, Lower Merion): the research's
    rank word only stays when the park's own name says regional / county / state / national."""
    if not category:
        return category
    word = re.search(r"\b(regional|county|state|national)\b", category, re.IGNORECASE)
    if word and not re.search(rf"\b{word.group(1)}\b", str(name or ""), re.IGNORECASE):
        out = re.sub(r"\s+", " ", re.sub(rf"\b{word.group(1)}\b", "", category, flags=re.IGNORECASE)).strip()
        return out[:1].upper() + out[1:] if out else "Park"
    return category


def town_district_names(facts: dict) -> list[str]:
    """Every real school district name the town record holds: the checked list, the research's own
    list and the Census borders on the town map."""
    names: list[str] = []
    for block in ((facts.get("public_schools") or {}).get("districts"), (facts.get("public_schools_research") or {}).get("districts"),
                  (facts.get("boundaries") or {}).get("school_districts")):
        for d in block or []:
            name = str((d.get("name") if isinstance(d, dict) else d) or "")
            if name and name not in names:
                names.append(name)
    return names


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
            **ai_config_service.model_options(model),
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
    from app.services.areas.town_places_check import fix_district_names
    districts = town_district_names(facts)

    async def fix(key: str, value: str) -> str:
        nonlocal changed
        new = await ai_fix_text(db, value, triggering_user_id) if use_ai else clean_text(value)
        new = fix_district_names(new, districts)  # shortened district names, fixed in the stored text too
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
