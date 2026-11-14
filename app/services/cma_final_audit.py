"""The in-site grader: one last AI read of a FINISHED CMA against its own facts (owner, 11/14).

Four outside audits in one day kept finding the same KIND of mistake - a sentence that contradicts the report's
own numbers ("Comp #2 sold above list within 10 days" for a sale that took 55 days and closed under asking; "the
lowest-priced sale in the set" for one that was not; "rarely sell for more than 98%" with nothing behind it).
Every one of those was fixed with a rule, and the rules stay. This pass is the net under them: a second model is
given the finished text and the facts the report was built from, and asked for what an auditor would write down.

What it may do:
  * REMOVE a sentence that contradicts the facts or claims something the facts do not show;
  * REPLACE a sentence with a corrected one that uses only figures already in the facts or in the sentence;
  * FLAG anything else for the agent ("Review before sending") - an omission, a section that disagrees with
    another, anything it is not sure of.
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

AUDIT_VERSION = "audit-v1"
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
                        "kind": {"type": "string", "enum": ["contradiction", "unsupported", "inconsistent", "omission"]},
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
    "Mark a sentence when:\n"
    "- it contradicts FACTS: the wrong comp number or address for a claim, a wrong price, date, day count, size, "
    "bath count, fee or tax figure, a wrong direction (above / below, larger / smaller, over / under asking), or a "
    "superlative that is not true (\"the lowest-priced sale\", \"the closest\", \"the newest\");\n"
    "- it states as fact something FACTS do not show: what buyers will do, why a seller acted, a market claim with "
    "no figure behind it, a feature or update not in the records;\n"
    "- two parts of TEXT disagree with each other;\n"
    "- (omission) FACTS show something material the client should be told and TEXT never mentions it: a home's own "
    "recent sale, a first-listed date weeks older than its days on market, a sale whose taxes or monthly fee are far "
    "from the subject's.\n\n"
    "Rules:\n"
    "- Never comment on whether the value is right, which comps were chosen, or the size of an adjustment. Those are "
    "not yours to judge.\n"
    "- Only mark what you can tie to a specific fact. If you are not sure, use action \"flag\".\n"
    "- \"remove\" when the sentence adds nothing once the error is gone; \"replace\" when a corrected sentence is "
    "worth keeping - and then use only figures already in FACTS or in the original sentence; \"flag\" for omissions "
    "and anything needing the agent's judgment.\n"
    "- Copy the sentence exactly as written, one sentence per finding.\n"
    "- A hedged sentence (\"may\", \"can\", \"less likely\") is not a claim of fact.\n"
    "- Most finished reports have zero to three findings. Return an empty list when nothing is wrong."
)


def _f(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def build_packet(subject: dict, comps: list, pricing: dict, mode: Optional[str] = None) -> dict:
    """FACTS + TEXT for the audit - only what the client report itself shows or rests on."""
    s = subject or {}
    est = (pricing or {}).get("ai_estimate") or {}
    trace = est.get("valuation_trace") or {}
    rows = {str(r.get("listing_key")): r for r in ((trace.get("anchor") or {}).get("comps") or [])}

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
        "listing_remarks": str(s.get("public_remarks") or "")[:1500] or None,
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
            {k: h.get(k) for k in ("street_address", "status", "list_price", "days_on_market", "monthly_fee", "match_score", "sqft")
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
    """(pricing, comps, applied, for_agent). Text edits only; anything that cannot be applied safely goes to the agent."""
    pricing, comps = copy.deepcopy(pricing or {}), copy.deepcopy(comps or [])
    applied, for_agent = [], []
    changes = 0
    for f in (findings or [])[:MAX_FINDINGS]:
        if not isinstance(f, dict):
            continue
        sentence = str(f.get("sentence") or "").strip()
        why = str(f.get("why") or "").strip()[:300]
        action = str(f.get("action") or "flag")
        where = str(f.get("where") or "")
        note = {"where": where, "kind": f.get("kind"), "sentence": sentence[:400], "why": why}
        if action == "flag" or not sentence or f.get("kind") == "omission":
            for_agent.append(note)
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


def agent_message(for_agent: list) -> Optional[str]:
    if not for_agent:
        return None
    bits = []
    for n in for_agent[:4]:
        q = f" (\"{n['sentence'][:140]}\")" if n.get("sentence") else ""
        bits.append(f"{n['why']}{q}")
    more = f" (+{len(for_agent) - 4} more)" if len(for_agent) > 4 else ""
    return "Review before sending: the final read of this report noted - " + "; ".join(bits) + more + "."


async def run(db, report, *, owner_id: str, config: dict, model: str, client, create, record_usage_event, add_spend,
              estimate_cost, lock_get, lock_put) -> Optional[dict]:
    """Audit report.pricing / report.comparables in place. Returns the stored audit record, or None when off / failed.
    The callables are passed in so this module has no import cycle with cma_ai_service and tests need no network."""
    if not enabled():
        return None
    try:
        pricing = dict(report.pricing or {})
        est = pricing.get("ai_estimate") or {}
        if not est or not est.get("value"):
            return None
        packet = build_packet(report.subject or {}, report.comparables or [], pricing, getattr(report, "mode", None))
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
                              "applied": applied, "for_agent": for_agent}
        msg = agent_message(for_agent)
        est["audit_review"] = {"message": msg, "notes": for_agent} if msg else None
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
