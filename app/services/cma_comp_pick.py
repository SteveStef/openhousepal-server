"""Which of the comp search's results are picked for the agent when the Comps step first opens.

Until 11/16 the wizard picked by Match score alone: the top three, then up to five at 60+. Two homes can match
alike on paper (size, beds, lot, age, distance, school district) and still sell 30-50% apart because one is dated
and one is redone, and nothing on the record says which. The asking price does: a home asking $245 a square foot
is in the league of the sales near $245, not the ones at $160.

So, for a subject that HAS an asking price, the order used for picking takes points off a sale whose sold $/sq ft
is far from the subject's asking $/sq ft. The Match score shown to the agent and the client is not changed.

Guards (owner, 11/16: "I get very scared ... a much different school district could climb the ranks"):
  1. A sale from another school district is never picked because of this - it can be in the pick only if the
     plain Match-score pick already had it.
  2. The new pick is kept only when the figure its sales point to ends up nearer the asking price than the plain
     pick's figure does; otherwise the plain pick stands.
  3. When the plain pick's sales already sit more than 20% from the asking price, the asking price does not get to
     choose the comps at all: that gap is the report's sign that the listing may be mispriced, and steering the
     comps toward the price would hide it (1459 N Hirst St: asked $135,000, sold $103,000 - the plain pick said
     $105,000, the steered one $132,000).
  4. No asking price (a seller's home not yet listed) -> the plain pick, exactly as before.

Free paired check on 294 past sales (60 ordinary homes, 60 townhouses, 60 condos, 39 bank-owned, 36 short and 39
estate sales; final value = the sales held inside the asking-price band, no AI; 11/16):
  plain pick                      typical miss 4.97%, average 7.55%, off by 15%+ 39, off by 25%+ 9
  this, with all the guards       typical miss 3.97%, average 6.73%, off by 15%+ 34, off by 25%+ 9; 82 closer / 31 further
  (without guard 3: 4.13% / 6.58%, 100 closer / 37 further, off by 25%+ 10. Treating another TOWN in the same school
  district as outside too was slightly worse - 4.27% / 6.65% - so the rule is the district alone. Matching on the
  MLS condition field instead of price level did not help: 4 closer / 14 further on ordinary homes.)
Known cost: a listing asking well above what it will sell for pulls pricier comps (homes that asked 10%+ over
their sale price: average miss 12.7% -> 13.3%). It is OFF unless CMA_PICK_PRICE_POINTS is set (6 = as tested).
"""
from __future__ import annotations

import logging
import math
import os
from typing import Optional

logger = logging.getLogger(__name__)

MAX_COMPS = 5
EXTRA_MIN_SCORE = 60           # the 4th and 5th pick need at least this
# OFF (0) BY DEFAULT. Owner, 11/16 night, on seeing a 94% match skipped for an 88% one in the Comps step: "The auto
# selected ones should be the ones with the highest % score ... the higher scores should be shown in highest to
# lowest and highest auto selected". So the pick is the plain Match-score pick, and the Comps step works it out
# itself again. The step below is kept, switched off, with its test numbers - 6 is the setting that was tested.
PRICE_LEVEL_POINTS = float(os.getenv("CMA_PICK_PRICE_POINTS", "0"))   # points off per 10% of $/sq ft gap ...
PRICE_LEVEL_FREE_GAP = 0.10                                            # ... past the first 10%
PRICE_LEVEL_MAX_ASK_GAP = 0.20     # guard 3: not used when the plain pick's sales are further than this from the asking price
# 1 = a sale in the same school district but another mailing town is treated like another district (never
# brought in by the price level). Owner 11/16: district only - "many towns are basically considered the same when
# they're served by the same school district".
TOWN_COUNTS_AS_OUTSIDE = os.getenv("CMA_PICK_TOWN_RULE", "0") == "1"


def pickable(c) -> bool:
    """Mirrors the wizard's isAutoPickable: a sale of another property type, one the value cannot rest on, or one
    outside the price band is shown but never picked for the agent."""
    d = getattr(c, "similarity_details", None) or {}
    return not d.get("property_type_incompatible") and d.get("valuation_eligible") is not False \
        and not d.get("outside_price_band")


def _sqft(c) -> Optional[float]:
    return getattr(c, "above_grade_finished_area", None) or getattr(c, "sqft", None)


def _outside(subject: dict, c) -> bool:
    from app.services import cma_ai_service as ai
    if ai._same_district(subject.get("school_district"), getattr(c, "school_district", None)) is False:
        return True
    return TOWN_COUNTS_AS_OUTSIDE and ai._same_place(subject.get("city"), getattr(c, "city", None)) is False


def _take(ordered: list, score) -> list:
    return [c for i, c in enumerate(ordered) if i < 3 or (i < MAX_COMPS and score(c) >= EXTRA_MIN_SCORE)]


def plain_pick(results: list, subject: dict) -> list:
    """The pick by Match score alone (what the wizard did before 11/16)."""
    from app.services import cma_service
    ranked = sorted((c for c in results if isinstance(getattr(c, "match_score", None), int)), key=lambda c: -c.match_score)
    return _take(cma_service.era_first([c for c in ranked if pickable(c)], subject.get("year_built")), lambda c: c.match_score)


def as_comparable(c, i: int) -> dict:
    from app.schemas.cma import CmaComparable
    d = c.model_dump()
    comp = {k: d[k] for k in set(CmaComparable.model_fields) & set(d)}
    comp.update({"source": "mls", "is_included": True, "order_index": i, "home_type": c.property_type,
                 "quality": {}, "strength": None, "commentary": None})
    return CmaComparable(**comp).model_dump()


def sales_figure(subject: dict, picks: list) -> Optional[float]:
    """What these sales point to by the adjustment table alone (no AI) - the same figure a report is built on."""
    from app.services import cma_ai_service as ai
    if not picks:
        return None
    comps = [as_comparable(c, i) for i, c in enumerate(picks)]
    outliers = ai._flag_psf_outliers(comps)
    _, anchor, _ = ai._apply_continuous_relevance_weighting(subject, comps, ai._build_evidence_by_key(subject, comps), outliers)
    return ai._as_float((anchor or {}).get("anchor_value"))


def suggest(subject: dict, results: list, *, asking_price: Optional[float] = None, subject_sqft: Optional[float] = None) -> dict:
    """The comps to pick for the agent: {"keys": [listing_key, ...] in pick order, "price_level": True when the
    asking price changed the pick, "reason": why not when it did not}. Never raises - any trouble = the plain pick."""
    from app.services import cma_service
    base = plain_pick(results, subject)
    out = {"keys": [c.listing_key for c in base], "price_level": False, "reason": None}
    try:
        ask = float(asking_price or 0)
        sq = float(subject_sqft or subject.get("above_grade_finished_area") or subject.get("sqft") or 0)
        if not PRICE_LEVEL_POINTS:
            out["reason"] = "switched off"; return out
        if ask <= 0 or sq <= 0:
            out["reason"] = "no asking price"; return out
        s_psf = ask / sq

        def score(c) -> float:
            s = float(c.match_score)
            price, csq = getattr(c, "sale_price", None), _sqft(c)
            if price and csq:
                s -= PRICE_LEVEL_POINTS * max(0.0, abs(math.log((price / csq) / s_psf)) - PRICE_LEVEL_FREE_GAP) * 10
            return s

        base_keys = {c.listing_key for c in base}
        ranked = sorted((c for c in results if isinstance(getattr(c, "match_score", None), int)), key=lambda c: -score(c))
        eligible = cma_service.era_first([c for c in ranked if pickable(c)], subject.get("year_built"))
        eligible = [c for c in eligible if c.listing_key in base_keys or not _outside(subject, c)]        # guard 1
        picks = _take(eligible, score)
        if not picks or {c.listing_key for c in picks} == base_keys:
            out["reason"] = "same homes either way"; return out
        base_fig = sales_figure(subject, base)
        if not base_fig:
            out["reason"] = "no sales figure"; return out
        if abs(base_fig - ask) / ask > PRICE_LEVEL_MAX_ASK_GAP:                                             # guard 3
            out["reason"] = "the sales are too far from the asking price to let it choose"; return out
        new_fig = sales_figure(subject, picks)                                                              # guard 2
        if not new_fig or abs(new_fig - ask) > abs(base_fig - ask):
            out["reason"] = "the plain pick's sales sit nearer the asking price"; return out
        return {"keys": [c.listing_key for c in picks], "price_level": True, "reason": None}
    except Exception:  # noqa: BLE001 - picking must never break the comp step
        logger.warning("CMA comp pick: price-level step failed, plain pick used", exc_info=True)
        out["reason"] = "error"
        return out


# ---- Price level INSIDE the Match % (owner, 11/17) ---------------------------------------------------------------
# "based on our findings we should always be using the private ranking ... agents won't understand to click the
# lower similarity score ... that's why we would have to move this into the score in a safe way". So for a subject
# with an asking price the Match % itself takes the price level into account, the list still runs highest to
# lowest and the highest are picked - one score, nothing hidden.
#   points off = MATCH_PRICE_POINTS per 10% that the sale's sold $/sq ft is away from the subject's asking $/sq ft,
#                the first 10% free (a sale at $162 a foot against $245 asked loses about 19 points).
#   school district, in the score: a sale from ANOTHER school district is never left ahead of an in-district sale
#                that scored at least as high before - it is held just under the lowest of those.
# Only sales (a verified sold price and a size) are touched; homes for sale / under contract keep their score.
# Free paired check, 1,303 past sales, final value (bt/r13score.py, r15score.py): see the numbers in the test notes.
# CMA_MATCH_PRICE_POINTS=0 switches it off (plain Match % everywhere).
MATCH_PRICE_POINTS = float(os.getenv("CMA_MATCH_PRICE_POINTS", "6"))


def price_points_off(ask_psf: Optional[float], price, sqft) -> float:
    try:
        if not (MATCH_PRICE_POINTS and ask_psf and price and sqft) or ask_psf <= 0 or float(price) <= 0 or float(sqft) <= 0:
            return 0.0
        return MATCH_PRICE_POINTS * max(0.0, abs(math.log((float(price) / float(sqft)) / ask_psf)) - PRICE_LEVEL_FREE_GAP) * 10
    except (TypeError, ValueError):
        return 0.0


_NOT_ON_MARKET = ("closed", "sold", "expired", "withdrawn", "cancel", "off market", "off-market", "off_market", "terminated")


def asking_price_now(subject: Optional[dict]) -> Optional[float]:
    """The subject's asking price only while it is for sale (live 11/19: a home that sold in 2024 carried its old listing
    price, and every comp's Match % was pulled toward it). Sold / expired / withdrawn / off market = no asking price.
    Any status that isn't plainly off the market (active, coming soon, pending, under contract, unknown) keeps it."""
    s = subject or {}
    status = " ".join(str(s.get(k) or "") for k in ("mls_status", "home_status", "status")).lower()
    if any(w in status for w in _NOT_ON_MARKET) and "active" not in status and "coming" not in status:
        return None
    try:
        return float(s.get("list_price")) if s.get("list_price") not in (None, "") else None
    except (TypeError, ValueError):
        return None


def apply_price_level(subject: dict, results: list, *, asking_price: Optional[float] = None, subject_sqft: Optional[float] = None) -> list:
    """The search results with price level inside each sale's Match %. Returns new candidate objects (the same
    order); a result that is not touched is returned as it is. Never raises - trouble = the plain scores."""
    try:
        ask = float(asking_price or 0)
        sq = float(subject_sqft or subject.get("above_grade_finished_area") or subject.get("sqft") or 0)
        if not MATCH_PRICE_POINTS or ask <= 0 or sq <= 0:
            return results
        from app.services import cma_ai_service as ai
        ask_psf = ask / sq
        new: dict = {}
        info: dict = {}
        scored = [c for c in results if isinstance(getattr(c, "match_score", None), int)]
        lowest_in_district = None
        for c in sorted(scored, key=lambda c: -c.match_score):               # highest plain score first
            off = price_points_off(ask_psf, getattr(c, "sale_price", None), _sqft(c)) if getattr(c, "sale_price", None) else 0.0
            value = c.match_score - off
            outside = ai._same_district(subject.get("school_district"), getattr(c, "school_district", None)) is False
            held = False
            if outside:
                if lowest_in_district is not None and value >= lowest_in_district:
                    value, held = lowest_in_district - 1, True
            else:
                lowest_in_district = value if lowest_in_district is None else min(lowest_in_district, value)
            value = int(max(0, round(value)))
            if value != c.match_score:
                new[id(c)] = value
                info[id(c)] = {"points_off": c.match_score - value, "match_before": c.match_score, "held_under_district": held,
                               "comp_price_per_sqft": round(c.sale_price / _sqft(c)) if getattr(c, "sale_price", None) and _sqft(c) else None,
                               "asking_price_per_sqft": round(ask_psf)}
        if not new:
            return results
        out = []
        for c in results:
            if id(c) not in new:
                out.append(c); continue
            d = {**(c.similarity_details or {}), "price_level": info[id(c)], "display_match_score": new[id(c)], "final_similarity_score": new[id(c)]}
            out.append(c.model_copy(update={"match_score": new[id(c)], "similarity_details": d}))
        return out
    except Exception:  # noqa: BLE001
        logger.warning("CMA match score: price-level step failed, plain scores used", exc_info=True)
        return results
