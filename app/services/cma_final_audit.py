"""The in-site grader: one last AI read of a FINISHED CMA against its own facts (owner, 11/14).

Four outside audits in one day kept finding the same KIND of mistake - a sentence that contradicts the report's
own numbers ("Comp #2 sold above list within 10 days" for a sale that took 55 days and closed under asking; "the
lowest-priced sale in the set" for one that was not; "rarely sell for more than 98%" with nothing behind it).
Every one of those was fixed with a rule, and the rules stay. This pass is the net under them: a second model is
given the finished text and the facts the report was built from, and asked for what an auditor would write down.

What it may do:
  * REMOVE a sentence that contradicts the facts, disagrees with another part of the report, or states what
    buyers or a seller will do / why they acted with nothing behind it;
  * REPLACE a sentence with a corrected one that uses only figures already in the facts or in the sentence;
  * FLAG anything else for the agent ("Review before sending") - an omission, anything it is not sure of.
What it may NOT do on its own word (v2, first live batch 11/14): remove a sentence only because it could not FIND
the detail in its packet. On six real reports it took out "It adds a deck and a wooded view" and "Possession is
listed as negotiable" - both true, both in the MLS record, neither in the packet it had been handed. Not seeing a
detail is not the same as the detail being wrong. Those findings ("unsupported") are now kept on the testing sheet
only: never applied, never shown to the agent. The packet also carries each home's full record and listing text.
What it may never do: change the value, the range, a comp, an adjustment or a share. It reads; it does not price.

It cannot see anything outside our data - a public site showing a different status, a price cut the MLS feed
never sent. It is best-effort: any failure leaves the report exactly as it was. Identical reports reuse the
stored result (no second charge). Off unless CMA_FINAL_AUDIT=1 - it adds an AI call to every report.
"""
import copy
import hashlib
import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

AUDIT_VERSION = "audit-v4"
MAX_FINDINGS = 12
MAX_TEXT_CHANGES = 6          # more than this and the rest go to the agent instead of being edited silently
_TEXT_FIELDS = ("methodology_summary", "offer_strategy_considerations", "listing_strategy_considerations",
                "inspection_concerns", "listing_prep_concerns")
_PRICING_TEXT_FIELDS = ("offer_suggestion_notes", "listing_strategy_notes", "inspection_concerns", "listing_prep_notes",
                        "pricing_commentary", "offer_strategy_commentary")
# sentences the audit may never remove or rewrite: they state the value itself
_PROTECTED_RE = re.compile(r"final fair market value|supported (?:valuation )?range|we recommend listing at|"
                           r"reasonable opening offer|Together: \$", re.I)


def enabled() -> bool:
    return os.getenv("CMA_FINAL_AUDIT", "0") == "1"


TOOL = {
    "name": "submit_cma_audit",
    "description": "Report what an auditor would write down about this finished CMA. Empty list when nothing is wrong.",
    "input_schema": {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array", "maxItems": MAX_FINDINGS,
                "items": {
                    "type": "object",
                    "properties": {
                        "where": {"type": "string", "description": "methodology | strategy | inspection | comp_note:<number>"},
                        "sentence": {"type": "string", "description": "The exact sentence from the text, copied word for word. Empty for an omission."},
                        "kind": {"type": "string", "enum": ["contradiction", "inconsistent", "speculation", "unsupported", "omission"]},
                        "why": {"type": "string", "description": "One plain sentence naming the fact it conflicts with or lacks."},
                        "action": {"type": "string", "enum": ["remove", "replace", "flag"]},
                        "replacement": {"type": "string", "description": "For replace: the corrected sentence. Use only figures that are in the facts or in the original sentence."},
                    },
                    "required": ["where", "sentence", "kind", "why", "action"],
                },
            },
        },
        "required": ["findings"],
    },
}

SYSTEM = (
    "You are the last reader of a comparative market analysis before a real estate agent sends it to a client. "
    "You are given FACTS (the records and numbers the report was built from) and TEXT (what the report says). "
    "Your job is to find sentences in TEXT that a careful outside auditor would mark as wrong, and nothing else.\n\n"
    "Mark a sentence, with one of these kinds:\n"
    "- contradiction: it conflicts with a specific fact in FACTS - the wrong comp number or address for a claim, a "
    "wrong price, date, day count, size, bath count, fee or tax figure, a wrong direction (above / below, larger / "
    "smaller, over / under asking), a weight word that the share_pct does not bear out (\"little weight\" for one of "
    "the largest shares), or a superlative that is not true (\"the lowest-priced sale\", \"the closest\");\n"
    "- inconsistent: two parts of TEXT disagree with each other (two different ranges, two different day counts, a "
    "reason given for the value that the value steps do not contain);\n"
    "- speculation: it states as fact what buyers will do, how they have reacted, why a seller acted, or a market "
    "claim with no figure behind it;\n"
    "- unsupported: it names a feature, update or term of a home that you cannot find anywhere in that home's facts. "
    "Each home's facts hold its whole listing_text and its mls_record (the MLS fields: heating, hot water, fireplace, "
    "flooring, possession, parking and so on) - read both to the end before you call anything unsupported; a fact "
    "stated in either one is verified, whatever words the report uses for it (\"new Anderson windows\" in the "
    "listing supports \"new windows\"; HotWater Electric supports \"the water heater is electric\"). The writers also "
    "saw the listing photos, which you cannot see: a statement about what a home looks like or visibly has "
    "(finishes, counters, cabinets, flooring, condition, a dated or updated room, a deck, balcony, patio or pergola) "
    "is NOT unsupported. What is left is the kind of claim no photo can show and no record states - a new roof or "
    "system, an age, a permit, an approval, a fee, a date;\n"
    "- omission: FACTS show something material the client should be told and TEXT never mentions it: a home's own "
    "recent sale, a first-listed date weeks older than its days on market, a sale whose taxes or monthly fee are far "
    "from the subject's.\n\n"
    "Rules:\n"
    "- Never comment on whether the value is right, which comps were chosen, or the size of an adjustment. Those are "
    "not yours to judge.\n"
    "- Only mark what you can tie to a specific fact. If you are not sure, use action \"flag\".\n"
    "- Check your own finding before you submit it: if, working it through, the sentence turns out to be true or "
    "to match FACTS, leave it out. Never submit a finding whose explanation ends up saying there is no error.\n"
    "- Not finding a detail in FACTS is not a contradiction. Use kind \"unsupported\" for it, never "
    "\"contradiction\".\n"
    "- \"remove\" when the sentence adds nothing once the error is gone; \"replace\" when a corrected sentence is "
    "worth keeping - and then use only figures already in FACTS or in the original sentence; \"flag\" for omissions "
    "and anything needing the agent's judgment.\n"
    "- A contradiction you can tie to a specific fact is yours to FIX, not to flag: a client must not be sent a "
    "sentence the report's own numbers show to be wrong. Replace it with a corrected sentence (keep what was true "
    "in it), or remove it. Use \"flag\" for a contradiction only when you cannot tell which side is right.\n"
    "- Copy the sentence exactly as written, one sentence per finding.\n"
    "- A hedged sentence (\"may\", \"can\", \"less likely\") is not a claim of fact.\n"
    "- Most finished reports have zero to three findings. Return an empty list when nothing is wrong."
)


# the rest of a home's saved record that the report's writers could see
_RECORD_FIELDS = (
    "city", "township", "school_district", "county", "property_type", "home_type", "structure_type", "architectural_style",
    "levels", "stories", "property_condition", "condition", "pool", "fireplace", "heating", "cooling", "water_source", "sewer",
    "roof_type", "construction_materials", "foundation_details", "garage_spaces", "lower_level_baths", "lower_level_bedrooms",
    "association_fee", "association_fee_frequency", "condo_fee", "condo_fee_frequency", "sale_type", "new_construction",
    "senior_community", "waterfront", "view", "parking_features", "exterior_features", "interior_features", "appliances",
)
# The WHOLE listing description. It was cut at 1,800 characters, and the outside audit of round 5 then read
# "could not verify" beside facts the listing states further down: 18 Colts Neck Dr's "all new Anderson windows &
# doors ... updated HVAC system" (character 2,244), 10 Eastburn Dr's "new septic tank and pump, new furnace" (2,157),
# 116 Acorn Way's "home gym" (2,861). A fact the listing states IS verified (owner, 11/14).
_REMARKS_CHARS = 6000
# The home's own Bright record, field for field (passed in by the caller - the saved report keeps only what the
# report page shows). The same round listed as unverifiable "the water heater is electric" (HotWater), "possession is
# negotiable" (Possession), "natural gas hot water" and "a gas fireplace" (FireplaceFeatures) - all in the record the
# writers were given and this reader was not. Features only: no agent names, phone numbers, ids or photo links.
_BRIGHT_FIELDS = (
    "AccessibilityFeatures", "Appliances", "ArchitecturalStyle", "AssociationAmenities", "AssociationFeeIncludes",
    "Basement", "BasementYN", "BuilderModel", "BuilderName", "CentralAirYN", "ConstructionMaterials", "Cooling",
    "CoolingFuel", "DoorFeatures", "Electric", "ExteriorFeatures", "Fencing", "FireplaceFeatures", "FireplaceYN",
    "FireplacesTotal", "Flooring", "FoundationDetails", "GarageType", "GarageYN", "Heating", "HeatingFuel", "HotWater",
    "InteriorFeatures", "LaundryType", "Levels", "LotFeatures", "LowerLevelsTotalBaths", "LowerLevelsTotalBedrooms",
    "MainLevelBedrooms", "MainLevelFullBaths", "NewConstructionYN", "NumAttachedGarageSpaces", "NumDrivewaySpaces",
    "OtherStructuresList", "OwnershipInterest", "ParkingFeatures", "ParkingTypes", "PatioAndPorchFeatures", "Pool",
    "PoolFeatures", "Possession", "PropertyCondition", "Roof", "RoomsTotal", "SaleType", "SecurityFeatures",
    "SeniorCommunityYN", "Sewer", "SpaFeatures", "StructureDesignType", "TotalGarageAndParkingSpaces",
    "UpperLevelsTotalBaths", "UpperLevelsTotalBedrooms", "Utilities", "View", "WaterAccessFeatures", "WaterSource",
    "WaterfrontFeatures", "WindowFeatures", "YearBuiltEffective", "Zoning",
)
# an explanation that talks itself out of the finding ("...so this is true", "no conflict") is not a finding
_SELF_CANCELLED_RE = re.compile(
    r"\b(?:so (?:this|it|that) is (?:true|accurate|correct|fine)|no (?:real |actual )?(?:conflict|error|contradiction|problem)|"
    r"there is no error|which matches|this matches|is accurate\b|is correct\b|so this is fine|not an error|"
    r"\b(?:is|are) true\b|this is true\b|flagged only lightly|low confidence|\bfine as\b|minor point only)", re.I)
# Sentences the engine itself writes from its own numbers (the value steps, the under-contract paragraph, the tax
# and fee gap notes, the first-listed line). The audit kept "finding" these on the first live batch - reading
# "not set above about $583,000 ... takes off $13,000 ... Together: $580,000" as an arithmetic slip, and the
# hedged tax-gap sentence as speculation. They are not the audit's to edit or to flag.
_ENGINE_SENTENCE_RE = re.compile(
    r"The value is therefore not set above|That takes off \$|Together: \$|How the value was reached|"
    r"Homes under contract \(market context|a gap that size usually means|monthly fee is \$|first entered the MLS|"
    r"is being sold at auction|the rate past local acreage sales support|points to a sale near \$|"
    r"listing text describes a finished basement", re.I)
# kinds the audit may act on by itself; "unsupported" (it could not FIND a detail) is never one of them
_EDIT_KINDS = {"contradiction", "inconsistent", "speculation"}


def _f(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def build_packet(subject: dict, comps: list, pricing: dict, mode: Optional[str] = None,
                 records: Optional[dict] = None) -> dict:
    """FACTS + TEXT for the audit - what the client report shows or rests on, plus each home's own Bright record
    (`records`: listing_key -> raw MLS row) so a fact the listing or the record states is never "unverified"."""
    s = subject or {}
    records = records or {}
    est = (pricing or {}).get("ai_estimate") or {}
    trace = est.get("valuation_trace") or {}
    rows = {str(r.get("listing_key")): r for r in ((trace.get("anchor") or {}).get("comps") or [])}

    tl = trace.get("status_timeline") or est.get("status_timeline") or {}

    def home(d, n=None):
        price, lp = _f(d.get("sale_price")) or _f(d.get("close_price")), _f(d.get("list_price"))
        out = {
            "address": d.get("street_address"), "sold_for": price, "final_asking_price": lp,
            "sold_vs_asking_pct": round((price / lp - 1) * 100, 1) if price and lp else None,
            "sold_date": str(d.get("close_date") or d.get("sale_date") or "")[:10] or None,
            "days_on_market": d.get("days_on_market"), "beds": d.get("beds"), "baths": d.get("baths"),
            "full_baths": d.get("full_baths"), "half_baths": d.get("half_baths"),
            "above_grade_sqft": d.get("above_grade_finished_area"), "below_grade_finished_sqft": d.get("below_grade_finished_area"),
            "reported_finished_sqft": d.get("sqft"), "year_built": d.get("year_built"), "lot_sqft": d.get("lot_size"),
            "garage": d.get("garage"), "basement": d.get("basement"), "annual_taxes": d.get("annual_taxes"),
            "distance_miles": d.get("distance_miles"), "subdivision": d.get("subdivision_name"),
        }
        # everything else the record holds about the home - what the writers were looking at (v2)
        for k in _RECORD_FIELDS:
            v = d.get(k)
            if v not in (None, "", [], {}) and k not in out:
                out[k] = v
        raw = records.get(str(d.get("listing_key"))) if d.get("listing_key") else None
        raw = raw if isinstance(raw, dict) else {}
        _remarks = str(d.get("public_remarks") or "").strip()
        _raw_remarks = str(raw.get("PublicRemarks") or "").strip()
        if len(_raw_remarks) > len(_remarks):      # a snapshot saved with a shortened description
            _remarks = _raw_remarks
        if _remarks:
            out["listing_text"] = _remarks[:_REMARKS_CHARS]
        _mls = {k: raw[k] for k in _BRIGHT_FIELDS if raw.get(k) not in (None, "", [], {})}
        if _mls:
            out["mls_record"] = _mls
        if n is not None:
            r = rows.get(str(d.get("listing_key"))) or {}
            fa = r.get("feature_adjustments") or {}
            out.update({
                "comp_number": n, "label": d.get("strength"), "ratings_vs_subject": d.get("quality") or None,
                "adjusted_price": r.get("size_adjusted_price"),
                "share_pct": round((r.get("normalized_weight") or 0) * 100, 1) if r else None,
                "adjustments": {k: round(v) for k, v in {"size": r.get("size_adjustment"), **{k: fa.get(k) for k in (
                    "below_grade", "baths", "garage", "lot", "time", "age", "condition", "location", "sewer", "road")}}.items()
                    if _f(v)},
            })
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}

    subj = home(s)
    subj.pop("sold_for", None); subj.pop("sold_vs_asking_pct", None); subj.pop("sold_date", None)
    subj.update({k: v for k, v in {
        "status": s.get("mls_status") or s.get("status"), "asking_price": _f(s.get("list_price")),
        "last_sale_price": _f(s.get("last_sale_price")), "last_sale_date": s.get("last_sale_date"),
        "property_type": s.get("property_type") or s.get("home_type"),
        "record": est.get("property_record"),
        "listing_timeline": {k: tl.get(k) for k in ("active_market_days", "current_status", "auction", "first_listed_gap",
                                                    "original_list_price", "previous_list_price", "possession")
                             if tl.get(k) not in (None, "", [], {})} or None,
    }.items() if v not in (None, "", [], {})})
    comp_facts, notes = [], {}
    for i, c in enumerate(comps or []):
        if not isinstance(c, dict) or not c.get("is_included", True):
            continue
        comp_facts.append(home(c, i + 1))
        if c.get("commentary") or c.get("ai_commentary"):
            notes[f"comp_note:{i + 1}"] = c.get("commentary") or c.get("ai_commentary")
    cc = est.get("current_competition") or {}
    bridge = est.get("value_bridge") or {}
    facts = {
        "report_for": "a buyer" if str(mode or "").lower() == "buyer" else "a seller (the homeowner)",
        "subject": subj, "comparable_sales": comp_facts,
        "value": {k: v for k, v in {
            "value": est.get("value"), "range_low": est.get("range_min"), "range_high": est.get("range_max"),
            "confidence": est.get("confidence"), "adjusted_sales_figure": bridge.get("adjusted_sales_value"),
            "steps_from_adjusted_sales_to_value": [{"step": st.get("label"), "amount": st.get("amount")} for st in bridge.get("steps") or []],
            "recommended_list_price": (pricing or {}).get("recommended_list_price"),
            "suggested_offer_low": (pricing or {}).get("suggested_offer_min"), "suggested_offer_high": (pricing or {}).get("suggested_offer_max"),
        }.items() if v not in (None, "", [], {})},
        "homes_for_sale_or_under_contract": [
            {k: h.get(k) for k in ("street_address", "status", "list_price", "days_on_market", "monthly_fee", "match_score", "sqft",
                                    "distance_miles", "beds", "baths", "year_built", "garage", "new_construction")
             if h.get(k) not in (None, "")} for h in (cc.get("homes") or [])],
    }
    text = {"methodology": est.get("methodology_summary"),
            "strategy": (pricing or {}).get("offer_suggestion_notes") or (pricing or {}).get("listing_strategy_notes"),
            "inspection": est.get("inspection_concerns") or est.get("listing_prep_concerns"), **notes}
    return {"facts": facts, "text": {k: v for k, v in text.items() if isinstance(v, str) and v.strip()}}


def fingerprint(packet: dict, model: str) -> str:
    return hashlib.sha256(json.dumps({"p": packet, "m": model, "v": AUDIT_VERSION}, sort_keys=True, default=str)
                          .encode("utf-8")).hexdigest()[:48]


_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set:
    out = set()
    for m in _NUM_RE.findall(str(text or "")):
        try:
            v = float(m.replace(",", ""))
        except ValueError:
            continue
        out.add(int(v) if v == int(v) else round(v, 3))
    return out


def replacement_is_safe(original: str, replacement: str, packet: dict) -> bool:
    """A corrected sentence may only use figures that are already in the facts or in the sentence it replaces."""
    if not replacement or not replacement.strip() or len(replacement) > max(400, 2 * len(original or "")):
        return False
    known = _numbers(original) | _numbers(json.dumps(packet.get("facts"), default=str))
    return all(n in known or n < 10 for n in _numbers(replacement))


def _edit_everywhere(obj, sentence: str, replacement: str, skip=("valuation_trace", "qa_flags", "final_audit", "audit_review")):
    """Swap `sentence` for `replacement` in every text of obj (the report keeps two copies of some texts)."""
    hits = 0

    def walk(o):
        nonlocal hits
        if isinstance(o, str):
            if sentence in o:
                hits += 1
                o = o.replace(sentence + " ", (replacement + " ") if replacement else "").replace(sentence, replacement)
                o = re.sub(r"[ \t]{2,}", " ", o)
                o = re.sub(r"\*\*[^*\n]{2,60}:\*\*\s*(?=\n|$)", "", o)      # a "**Heading:**" left with nothing under it
                return re.sub(r"\n{3,}", "\n\n", o).strip()
            return o
        if isinstance(o, list):
            return [walk(v) for v in o]
        if isinstance(o, dict):
            return {k: (v if k in skip else walk(v)) for k, v in o.items()}
        return o
    return walk(obj), hits


def apply_findings(pricing: dict, comps: list, findings: list, packet: dict) -> tuple:
    """(pricing, comps, applied, for_agent). Text edits only; anything that cannot be applied safely goes to the agent.
    Findings that say "I could not find this detail" (kind unsupported) are skipped here - see unverified_findings."""
    pricing, comps = copy.deepcopy(pricing or {}), copy.deepcopy(comps or [])
    applied, for_agent = [], []
    changes = 0
    position = _pricing_position_text(pricing)
    for f in (findings or [])[:MAX_FINDINGS]:
        if not isinstance(f, dict):
            continue
        sentence = str(f.get("sentence") or "").strip()
        why = str(f.get("why") or "").strip()[:300]
        action = str(f.get("action") or "flag")
        where = str(f.get("where") or "")
        kind = str(f.get("kind") or "")
        note = {"where": where, "kind": kind, "sentence": sentence[:400], "why": why}
        if _SELF_CANCELLED_RE.search(why):
            continue                              # the explanation itself says nothing is wrong
        if sentence and _ENGINE_SENTENCE_RE.search(sentence):
            continue                              # the engine's own computed line
        if kind == "unsupported":
            continue                              # not seeing a detail is not the detail being wrong
        if action == "flag" or not sentence or kind == "omission" or kind not in _EDIT_KINDS:
            for_agent.append(note)
            continue
        if sentence and position and sentence in position:
            for_agent.append(note)                # the Pricing Position is built from the report's own numbers
            continue
        replacement = str(f.get("replacement") or "").strip() if action == "replace" else ""
        if _PROTECTED_RE.search(sentence) or changes >= MAX_TEXT_CHANGES or \
                (action == "replace" and not replacement_is_safe(sentence, replacement, packet)):
            for_agent.append(note)
            continue
        m = re.fullmatch(r"comp_note:(\d+)", where)
        if m:
            idx = int(m.group(1)) - 1
            if not (0 <= idx < len(comps)) or not isinstance(comps[idx], dict):
                for_agent.append(note)
                continue
            c = comps[idx]
            own = c.get("commentary") and c.get("ai_commentary") and c["commentary"] != c["ai_commentary"]
            hits = 0
            for fld in ("ai_commentary",) if own else ("ai_commentary", "commentary"):
                if isinstance(c.get(fld), str) and sentence in c[fld]:
                    c[fld], h = _edit_everywhere(c[fld], sentence, replacement)
                    hits += h
        else:
            pricing, hits = _edit_everywhere(pricing, sentence, replacement)
        if not hits:
            for_agent.append(note)      # the model did not copy the sentence exactly - nothing was touched
            continue
        changes += 1
        applied.append({**note, "action": action, "replacement": replacement or None})
    return pricing, comps, applied, for_agent


def unverified_findings(findings: list) -> list:
    """What the audit could not FIND in the records (kind unsupported). Never applied to the text; listed for the
    agent to check (see agent_message) and on the testing sheet."""
    out = []
    for f in (findings or [])[:MAX_FINDINGS]:
        if isinstance(f, dict) and str(f.get("kind") or "") == "unsupported" \
                and not _SELF_CANCELLED_RE.search(str(f.get("why") or "")):
            out.append({"where": str(f.get("where") or ""), "kind": "unsupported",
                        "sentence": str(f.get("sentence") or "").strip()[:400], "why": str(f.get("why") or "").strip()[:300]})
    return out


def _pricing_position_text(pricing: dict) -> str:
    """The seller report's computed Pricing Position paragraph - the audit may flag it, never edit it."""
    for k in ("listing_strategy_notes",):
        for para in re.split(r"\n\s*\n", str((pricing or {}).get(k) or "")):
            if para.strip().startswith("**Pricing Position:**"):
                return para
    return ""


def agent_message(for_agent: list, unverified: Optional[list] = None) -> Optional[str]:
    """One note for the agent. `unverified` = sentences whose detail is in neither the listing text nor the MLS
    record. Two outside audits (rounds 5 and 6) called it the most important open point that such a sentence stayed
    in the client's report with nobody told. They are never removed (the detail may come from the listing photos,
    which this reader cannot see) - the agent, who can look at the photos, is told which ones to check."""
    if not for_agent and not unverified:
        return None
    parts = []
    if for_agent:
        bits = []
        for n in for_agent[:4]:
            q = f" (\"{n['sentence'][:140]}\")" if n.get("sentence") else ""
            bits.append(f"{n['why']}{q}")
        more = f" (+{len(for_agent) - 4} more)" if len(for_agent) > 4 else ""
        parts.append("the final read of this report noted - " + "; ".join(bits) + more + ".")
    if unverified:
        bits = [f"\"{u['sentence'][:140]}\" ({u['why'][:120].rstrip('.')})" for u in unverified[:4] if u.get("sentence")]
        if bits:
            more = f" (+{len(unverified) - 4} more)" if len(unverified) > 4 else ""
            lead = ("These details are in neither the listing text nor the MLS record - check each against the photos, and "
                    "take out any you cannot confirm: ")
            parts.append((lead if for_agent else lead[:1].lower() + lead[1:]) + "; ".join(bits) + more + ".")
    return ("Review before sending: " + " ".join(parts)) if parts else None


async def run(db, report, *, owner_id: str, config: dict, model: str, client, create, record_usage_event, add_spend,
              estimate_cost, lock_get, lock_put, records: Optional[dict] = None) -> Optional[dict]:
    """Audit report.pricing / report.comparables in place. Returns the stored audit record, or None when off / failed.
    The callables are passed in so this module has no import cycle with cma_ai_service and tests need no network."""
    if not enabled():
        return None
    try:
        pricing = dict(report.pricing or {})
        est = pricing.get("ai_estimate") or {}
        if not est or not est.get("value"):
            return None
        packet = build_packet(report.subject or {}, report.comparables or [], pricing, getattr(report, "mode", None),
                              records=records)
        if not packet["text"]:
            return None
        fp = fingerprint(packet, model)
        cached = await lock_get(db, getattr(report, "id", None), "audit", fp)
        cost = 0.0
        if cached and isinstance(cached.get("raw"), dict):
            findings = cached["raw"].get("findings") or []
            reused = True
        else:
            resp = await create(
                client, effort="low", model=model, max_tokens=3000, system=SYSTEM, tools=[TOOL],
                tool_choice={"type": "tool", "name": TOOL["name"]},
                messages=[{"role": "user", "content": "FACTS:\n" + json.dumps(packet["facts"], default=str) +
                           "\n\nTEXT:\n" + json.dumps(packet["text"], default=str)}])
            block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use" and b.name == TOOL["name"]), None)
            findings = (block.input or {}).get("findings") if block and isinstance(block.input, dict) else None
            usage = getattr(resp, "usage", None)
            tin, tout = int(getattr(usage, "input_tokens", 0) or 0), int(getattr(usage, "output_tokens", 0) or 0)
            cost = estimate_cost(config, model, tin, tout)
            await record_usage_event(db, owner_id, None, model, tin, tout, cost, 0, False, None, event_type="cma_audit")
            await add_spend(db, ["cma_ai_global", "platform_emergency"], cost)
            if findings is None:
                return None
            reused = False
            await lock_put(db, getattr(report, "id", None), "audit", fp, {"findings": findings})
        new_pricing, new_comps, applied, for_agent = apply_findings(pricing, report.comparables or [], findings, packet)
        est = dict(new_pricing.get("ai_estimate") or {})
        est["final_audit"] = {"version": AUDIT_VERSION, "model": model, "reused": reused, "cost_usd": round(cost, 4),
                              "applied": applied, "for_agent": for_agent,
                              "unverified": unverified_findings(findings)}
        _unv = est["final_audit"]["unverified"]
        msg = agent_message(for_agent, _unv)
        est["audit_review"] = {"message": msg, "notes": for_agent, "unverified": _unv} if msg else None
        flags = list(est.get("qa_flags") or [])
        for a in applied:
            flags.append(f"Final read: {'reworded' if a['action'] == 'replace' else 'removed'} a sentence in {a['where']} - {a['why']}")
        est["qa_flags"] = flags
        new_pricing["ai_estimate"] = est
        report.pricing = new_pricing
        report.comparables = new_comps
        return est["final_audit"]
    except Exception:  # noqa: BLE001 - the last read never fails a finished, paid report
        logger.warning("CMA final audit failed", exc_info=True)
        return None
