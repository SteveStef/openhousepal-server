"""Zero-cost stand-ins for the two AI providers, used ONLY by accuracy-test DRY RUNS.

They let the whole CMA pipeline run for real - address lookup, comp search + auto-pick, photo
download and prompt building, the deterministic valuation, every QA/consistency pass, saving -
without paying for a model. Every request they receive is captured on the time machine so the
leak scan can prove nothing about the subject's real sale reached ANY model-bound input. The
numbers they return are placeholders (median comp price), not a valuation - a dry run proves the
plumbing and the no-leak guarantee, never accuracy.
"""
from __future__ import annotations

import statistics
from types import SimpleNamespace
from typing import Any, Optional

from app.services import cma_time_machine

_STUB_TEXT = (
    "**General Inspection:** Dry-run placeholder text - no AI model was called for this report. "
    "Verify the structure, roof, mechanical systems and any listed updates with a licensed inspector. "
    "**Records:** Ask for permits and service records for any work the listing describes. "
    "**Utilities:** Confirm water, sewer and heating fuel with the seller's disclosure."
)


def _median_price(ctx: dict) -> Optional[float]:
    prices = [float(c["price"]) for c in (ctx.get("comps") or []) if c.get("price")]
    return statistics.median(prices) if prices else (ctx.get("list_price") or None)


def _subject_estimate(ctx: dict) -> dict:
    v = _median_price(ctx) or 500000.0
    return {
        "value": round(v), "range_pct": 6, "confidence": "medium",
        "methodology_summary": "Dry-run placeholder: no AI model was called. The value shown is the median comparable sale price.",
        "value_drivers": [], "qualitative_adjustments": [], "premium_improvements": [],
        "inspection_concerns": _STUB_TEXT, "offer_strategy_considerations": "Dry-run placeholder offer guidance - no AI model was called.",
        "listing_strategy_considerations": "**Competition:** Dry-run placeholder - no AI model was called.",
        "listing_prep_concerns": _STUB_TEXT,
    }


class _Messages:
    def __init__(self, ctx: dict):
        self._ctx = ctx

    async def create(self, **kwargs: Any):
        tm = cma_time_machine.current()
        if tm is not None:
            tm.captured.append({"provider": "anthropic", "system": kwargs.get("system"),
                                "messages": kwargs.get("messages"),
                                "tool": [t.get("name") for t in (kwargs.get("tools") or [])]})
        tools = kwargs.get("tools") or []
        name = tools[0].get("name") if tools else None
        usage = SimpleNamespace(input_tokens=0, output_tokens=0)
        if name == "submit_cma_analysis":
            comps = [{"listing_key": c["listing_key"], "note": f"{c.get('address') or 'This sale'} is a dry-run placeholder note.",
                      "strength": "supporting", "quality": {}} for c in (self._ctx.get("comps") or [])]
            data = {"comps": comps, "subject_estimate": _subject_estimate(self._ctx)}
        elif name == "submit_cma_estimate":
            data = {"subject_estimate": _subject_estimate(self._ctx)}
        elif name:
            props = ((tools[0].get("input_schema") or {}).get("properties") or {})
            data = {k: _STUB_TEXT for k in props}
        else:
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="{}")], usage=usage, stop_reason="end_turn")
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=name, input=data)], usage=usage, stop_reason="tool_use")


class StubAnthropic:
    def __init__(self, ctx: dict):
        self.messages = _Messages(ctx)


def stub_reasoning(packet: dict) -> dict:
    tm = cma_time_machine.current()
    if tm is not None:
        tm.captured.append({"provider": "openai", "packet": packet})
    comps = packet.get("comparables") or []
    prices = [float(c["sale_price"]) for c in comps if c.get("sale_price")]
    det = (packet.get("deterministic_evidence") or {}).get("deterministic_anchor")
    v = round(float(det) if det else (statistics.median(prices) if prices else 500000.0))
    lo, hi = round(v * 0.94), round(v * 1.06)
    first = str(comps[0]["comp_id"]) if comps else None
    assess = {"classification": "reasonable", "reason": "Dry-run placeholder."}
    return {
        "independent_preliminary_fmv": v, "independent_preliminary_range_low": lo,
        "independent_preliminary_range_high": hi, "independent_preliminary_confidence": "medium",
        "final_fmv": v, "range_low": lo, "range_high": hi, "confidence": "medium",
        "anchor_assessment": assess, "v10_assessment": assess,
        "primary_comps": [{"comp_id": first, "role": "direct", "reason": "Dry-run placeholder."}] if first else [],
        "floor_comps": [], "ceiling_comps": [],
        "comp_roles": [{"comp_id": str(c["comp_id"]), "role": "direct_analog", "reason": "Dry-run placeholder."} for c in comps],
        "major_value_drivers": [], "major_value_drags": [],
        "market_exposure_assessment": "Dry-run placeholder.",
        "why_final_differs_from_anchor": "Dry-run placeholder.",
        "why_final_differs_from_v10": "Dry-run placeholder.",
        "final_valuation_summary": "Dry-run placeholder: no AI model was called for this report.",
    }
