"""AI reads the listings for one question (owner, 11/08: "no matter how smart we make the filter, if
it's not AI it won't catch the right stuff"). Word matching missed "owner's retreat on the main level",
"1st floor bedroom" vs "first-floor", "one-floor living" - so the chat's model reads each home's
listing description, MLS features and bedroom-by-floor counts and says yes / maybe for the question,
with the exact words that show it.

Cheap on purpose: Haiku 5.5, about 400 words of input per home, 20 homes per call, only the hits come
back. A 100-home Showcase is about half a cent; READ_CAP (300 homes) is the most one question reads.
The cost is worked out BEFORE anything runs (estimate) and checked against what's left of the agent's
chat allowance (room) - a read that wouldn't fit never starts (owner, 11/08). The real cost is
counted on the agent's Chat usage bar like any chat answer.

Every quote is checked against the listing's real text; one that isn't really there is dropped (the
"why" stays), so the card never shows words the listing didn't say."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, List, Optional, Tuple

import anthropic

from app.config.logging import get_logger
from app.models.database import Property, User
from app.services.ai.ai_config_service import ai_config_service

logger = get_logger(__name__)

READ_CAP = 300      # the most homes one question reads
BATCH = 10          # homes per AI call - at 20 it skimmed and missed plain "3-car garage" lines (11/08 accuracy check)
PARALLEL = 10       # AI calls at once - 300 homes = 30 calls in 3 waves, well inside the chat's 60 seconds
_DESC_MAX = 1800    # characters of description per home (90% of listings are shorter)
_FEAT_MAX = 700
_CHARS_PER_TOKEN = 3.0   # cautious: the 11/08 check ran ~35% over a 3.6 estimate
_PROMPT_TOKENS = 480        # the instructions, once per call
_OUT_TOKENS_PER_HOME = 115  # the model's own thinking counts as output too (measured ~100 a home on 11/08), with room to spare

_client: Optional[anthropic.AsyncAnthropic] = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


# Every Bright field we keep that can answer a "which homes have ..." question (owner, 11/08: "use both
# the description and what we pull from Bright"). Feature lists come from ai_tools._feature_strings;
# these are the facts beside them. Agent / office / tax / photo / id fields are left out.
_BRIGHT_FACTS = (
    ("BathroomsFull", "full baths"), ("BathroomsHalf", "half baths"), ("UpperLevelsTotalBedrooms", "upper-level bedrooms"),
    ("UpperLevelsTotalBaths", "upper-level baths"), ("LowerLevelsTotalBedrooms", "lower-level bedrooms"), ("LowerLevelsTotalBaths", "lower-level baths"),
    ("StoriesTotal", "stories"), ("RoomsTotal", "rooms"), ("YearBuilt", "built"), ("YearBuiltEffective", "renovated"),
    ("ArchitecturalStyle", "style"), ("StructureDesignType", "design"), ("PropertyCondition", "condition"), ("NewConstructionYN", "new construction"),
    ("SeniorCommunityYN", "55+ community"), ("Pool", "pool"), ("GarageSpaces", "garage spaces"), ("NumAttachedGarageSpaces", "attached garage spaces"),
    ("GarageType", "garage"), ("GarageSQFT", "garage sqft"), ("ParkingTypes", "parking"), ("NumDrivewaySpaces", "driveway spaces"),
    ("BasementFinishedPercent", "basement finished %"), ("BelowGradeFinishedArea", "finished below-grade sqft"),
    ("AboveGradeFinishedArea", "finished above-grade sqft"), ("LotSizeAcres", "acres"), ("WaterfrontYN", "waterfront"),
    ("FireplacesTotal", "fireplaces"), ("LaundryType", "laundry"), ("Roof", "roof"), ("ConstructionMaterials", "built of"),
    ("FoundationDetails", "foundation"), ("WindowFeatures", "windows"), ("DoorFeatures", "doors"), ("OtherStructuresList", "other buildings"),
    ("HeatingFuel", "heating fuel"), ("HotWater", "hot water"), ("WaterSource", "water"), ("Sewer", "sewer"), ("Utilities", "utilities"),
    ("Electric", "electric"), ("AssociationFee", "HOA fee"), ("AssociationFeeFrequency", "HOA paid"), ("AssociationFeeIncludes", "HOA covers"),
    ("PetsAllowed", "pets"), ("SubdivisionName", "neighborhood"), ("SchoolDistrictName", "school district"), ("ElementarySchool", "elementary"),
    ("MiddleOrJuniorSchool", "middle school"), ("HighSchool", "high school"), ("Zoning", "zoning"),
)
_FACTS_MAX = 900


def _bright_facts(raw: dict) -> str:
    out = []
    for k, label in _BRIGHT_FACTS:
        v = raw.get(k)
        if v in (None, "", [], 0) or v is False and not k.endswith("YN"):
            continue
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v if x)
        elif isinstance(v, bool):
            v = "yes" if v else "no"
        elif isinstance(v, float) and v.is_integer():
            v = int(v)
        out.append(f"{label}: {v}")
    return " | ".join(out)[:_FACTS_MAX]


def home_text(p: Property) -> Tuple[str, str]:
    """(what the AI reads for this home, every word it may quote from)."""
    from app.services.ai.ai_tools import _feature_strings, _floor_facts
    raw = p.raw_mls_data if isinstance(p.raw_mls_data, dict) else {}
    desc = re.sub(r"\s+", " ", p.description or "").strip()
    feats = "; ".join(_feature_strings(p))
    fact, one_level = _floor_facts(p)
    facts = [f"{int(p.bedrooms)} bd" if p.bedrooms else "", f"{p.bathrooms:g} ba" if p.bathrooms else "",
             f"{int(p.living_area):,} sqft" if p.living_area else "", ("one-level home" if one_level else ""), fact or ""]
    levels = raw.get("Levels") or p.levels
    if levels and not one_level:
        facts.append(f"levels: {', '.join(str(x) for x in levels) if isinstance(levels, list) else levels}")
    for k, label in (("MainLevelBedrooms", "main-level bedrooms"), ("MainLevelFullBaths", "main-level full baths")):
        if isinstance(raw.get(k), (int, float)):
            facts.append(f"{label}: {int(raw[k])}")
    more = _bright_facts(raw)
    block = (f"Facts: {' | '.join(f for f in facts if f)}\n"
             + (f"More MLS facts: {more}\n" if more else "")
             + (f"MLS features: {feats[:_FEAT_MAX]}\n" if feats else "")
             + f"Description: {desc[:_DESC_MAX] if desc else '(none)'}")
    return block, f"{desc} {feats} {fact or ''} {more}"


def estimate_usd(config: dict, total_chars: int, homes: int) -> float:
    """What reading `homes` listings (`total_chars` of text in all) costs - worked out before anything runs."""
    if homes <= 0:
        return 0.0
    calls = -(-homes // BATCH)
    tokens_in = int(total_chars / _CHARS_PER_TOKEN) + calls * _PROMPT_TOKENS
    per_call_in, per_call_out = tokens_in // calls, (homes * _OUT_TOKENS_PER_HOME) // calls
    return calls * ai_config_service.estimate_cost_usd(config, config["model"], per_call_in, per_call_out)


def estimate_for(config: dict, homes: List[Property]) -> float:
    return estimate_usd(config, sum(len(home_text(p)[0]) for p in homes), len(homes))


async def room(db, user: User, config: dict) -> Tuple[Optional[float], float]:
    """(dollars left on the agent's chat allowance - None when it has no limit, the allowance)."""
    from app.services.ai import ai_rate_limit_service as rl
    snap = await rl.chat_usage_snapshot(db, user.id, config, user.plan_tier)
    limit = float(snap.get("limit_usd") or 0)
    if user.id in rl._MONTHLY_BUDGET_UNLIMITED_OWNER_IDS or not limit:
        return None, limit
    return max(0.0, limit - float(snap.get("used_usd") or 0)), limit


def percent(usd: float, limit: float) -> float:
    """A cost as a percent of the allowance, one decimal (never a dollar figure on screen)."""
    return round(usd / limit * 100, 1) if limit else 0.0


def percent_text(p: float) -> str:
    return "under 0.1%" if p < 0.1 else f"about {p:g}%"


_SYSTEM = (
    "You read real-estate listings for an agent who asked a question about them. Think like a sharp agent, not a word matcher. "
    "For each numbered home decide:\n"
    "- yes: the listing clearly has what was asked, in any wording ('1st floor', 'main level', 'entry level', 'owner's suite on "
    "the first floor' all mean first floor; MLS facts like 'main-level bedrooms: 1' or 'Entry Level Bedroom' count). A one-level "
    "home ('one-story', 'ranch', 'single-level living', 'everything on one floor', 'one-level home' in Facts) has every room on the "
    "first floor - so anything asked for on the first floor (a bedroom, the primary, high ceilings, laundry) is yes unless it says it is on another level. Never call a one-level home maybe just because "
    "it doesn't say 'first floor' - that is a yes.\n"
    "- Numbers mean AT LEAST: '9-foot ceilings' = 9 ft or higher (10 ft is yes), a '2-car garage' = 2 or more spaces (a 3- or "
    "4-car garage is yes), '4 bedrooms' or 'half an acre' = that or more. Only words like 'exactly', 'only', 'at most', 'under' "
    "or 'no more than' cap it. Less than asked is no.\n"
    "- maybe: it only hints at it or it's partly there (a community pool for a pool, 'room to add', a bedroom on the first floor "
    "but the question asked for the primary, high ceilings with no height given).\n"
    "- leave the home out when it doesn't mention it, or says the opposite ('no first-floor bedroom', 'pool table', a lower-level "
    "suite for a main-floor ask).\n"
    "Read every home to the end of its description - the line you need is often near the end.\n"
    "For each home that mentions the subject at all, give: n = its number; q = the SHORTEST exact words from that home's text "
    "that prove it (copied character for character, 3 to 10 words - just the key part, not the whole sentence; empty if it's only "
    "from Facts); why = at most 8 plain words; and LAST v = yes, maybe or no, decided from q and why (an unfinished basement for a "
    "finished one or a community pool for a private one is no or maybe - never yes). Leave out homes that don't mention it at all.\n"
    'Answer with JSON only, no other text: {"hits":[{"n":3,"q":"...","why":"...","v":"yes"}]} - {"hits":[]} when none fit.'
)

# The card shows the AI's few key words highlighted inside a short piece of the sentence around them
# (owner, 11/08: listing sentences can be very long). These marks wrap the key words.
HL_OPEN, HL_CLOSE = "⟦", "⟧"
_PAD_WORDS = 7
_ALNUM = set("abcdefghijklmnopqrstuvwxyz0123456789")


def _find(text: str, q: str) -> Optional[Tuple[int, int]]:
    """Where `q` sits in `text` (ignoring case, spacing and punctuation), as (start, end) in `text`."""
    chars, idx, gap = [], [], True
    for i, c in enumerate(text.lower()):
        if c in _ALNUM:
            chars.append(c); idx.append(i); gap = False
        elif not gap:
            chars.append(" "); idx.append(i); gap = True
    nq = _norm(q)
    pos = "".join(chars).find(nq) if nq else -1
    if pos < 0:
        return None
    return idx[pos], idx[pos + len(nq) - 1] + 1


def snippet(text: str, q: str) -> str:
    """The key words marked, with a few words either side (stopping at the sentence's edge), '...' where cut."""
    at = _find(text, q)
    if not at:
        return ""
    s, e = at
    while s > 0 and not text[s - 1].isspace():
        s -= 1
    while e < len(text) and text[e].isalnum():
        e += 1
    t = e
    while t < len(text) and not text[t].isspace():
        t += 1                      # punctuation stuck to the last word (", " "." ")")
    ends = lambda w: bool(w) and (w[-1] in ".!?;" or w == "|")   # a sentence or an MLS field ends
    before, after = text[:s].split(), text[t:].split()
    pre = []
    for w in reversed(before[-_PAD_WORDS:]):
        if ends(w):
            break
        pre.insert(0, w)
    cut_start = len(pre) < len(before) and not ends(before[len(before) - len(pre) - 1])
    post, cut_end = [], False
    if not ends(text[e:t]):
        for w in after[:_PAD_WORDS]:
            if w == "|":
                break
            post.append(w)
            if ends(w):
                break
        else:
            cut_end = len(after) > _PAD_WORDS
    out = " ".join(pre + [HL_OPEN + text[s:e] + HL_CLOSE + text[e:t]] + post)
    return ("..." if cut_start else "") + out + ("..." if cut_end else "")


def plain(s: str) -> str:
    return (s or "").replace(HL_OPEN, "").replace(HL_CLOSE, "")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


async def _read_batch(config: dict, question: str, batch: List[Tuple[Property, str, str]], sem: asyncio.Semaphore) -> Tuple[Dict[str, dict], dict, bool]:
    body = "\n\n".join(f"=== Home {i + 1} ===\n{text}" for i, (_, text, _) in enumerate(batch))
    user_msg = f"The agent's question: {question}\n\n{body}"
    async with sem:
        for attempt in range(2):
            try:
                resp = await asyncio.wait_for(_get_client().messages.create(
                    model=config["model"], max_tokens=min(4000, 90 * len(batch) + 200), system=_SYSTEM,
                    messages=[{"role": "user", "content": user_msg}],
                    **ai_config_service.model_options(config["model"], "medium"),
                ), timeout=28)
                break
            except Exception as e:  # noqa: BLE001 - one retry, then those homes fall back to word matching
                if attempt:
                    logger.warning("home reader batch failed: %s", e)
                    return {}, {"in": 0, "out": 0, "usd": 0.0}, False
    used = {"in": resp.usage.input_tokens, "out": resp.usage.output_tokens,
            "usd": ai_config_service.estimate_cost_usd(config, config["model"], resp.usage.input_tokens, resp.usage.output_tokens)}
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    # Each {"n":..} on its own, so one bad line (or an answer cut short) doesn't lose the rest.
    hits = []
    for piece in re.findall(r"\{[^{}]*\}", text):
        try:
            h = json.loads(piece)
        except ValueError:
            continue
        if isinstance(h, dict) and "n" in h:
            hits.append(h)
    if not hits and not re.search(r'"hits"\s*:\s*\[\s*\]', text):
        logger.warning("home reader: unreadable answer: %r", text[:200])
        return {}, used, False
    out: Dict[str, dict] = {}
    for h in hits:
        try:
            n = int(h.get("n")) - 1
        except (TypeError, ValueError):
            continue
        if not 0 <= n < len(batch) or h.get("v") not in ("yes", "maybe"):
            continue
        p, _, source = batch[n]
        quote = re.sub(r"\s+", " ", str(h.get("q") or "")).strip().strip('"').strip()
        desc = re.sub(r"\s+", " ", p.description or "")
        shown = snippet(desc, quote) if quote else ""
        src = "Description" if shown else "MLS"
        if quote and not shown:
            shown = snippet(re.sub(r"\s+", " ", source), quote)   # "" when it isn't really in the listing - never shown
        out[p.id] = {"v": h["v"], "quote": shown[:320], "src": src, "why": str(h.get("why") or "").strip()[:80]}
    return out, used, True


async def read(config: dict, question: str, homes: List[Property]) -> Dict[str, Any]:
    """{verdicts: {property_id: {v, quote, why}}, read: [ids the AI read], failed: [ids it couldn't], usd, in, out}."""
    items = [(p, *home_text(p)) for p in homes[:READ_CAP]]
    batches = [items[i:i + BATCH] for i in range(0, len(items), BATCH)]
    sem = asyncio.Semaphore(PARALLEL)
    results = await asyncio.gather(*(_read_batch(config, question, b, sem) for b in batches))
    verdicts: Dict[str, dict] = {}
    read_ids, failed, usd, tin, tout = [], [], 0.0, 0, 0
    for b, (v, used, ok) in zip(batches, results):
        verdicts.update(v)
        (read_ids if ok else failed).extend(p.id for p, _, _ in b)
        usd += used["usd"]
        tin += used["in"]
        tout += used["out"]
    return {"verdicts": verdicts, "read": read_ids, "failed": failed, "usd": usd, "in": tin, "out": tout}


def found_lines(v: dict) -> List[str]:
    """A verdict as the card's "why it matched" lines."""
    if v.get("quote"):
        head = "Description" if v.get("src") != "MLS" else "MLS"
        return [f"{head}: \"{v['quote']}\""] + ([v["why"]] if v.get("why") else [])
    return [v.get("why") or "The listing says so"]
