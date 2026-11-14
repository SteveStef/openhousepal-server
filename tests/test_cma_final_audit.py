"""The in-site grader (app/services/cma_final_audit.py): what it may change, what it may not, and what it costs.

No network and no database: the model is a stand-in that returns the findings a test gives it.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.services import cma_final_audit as audit


def _report():
    comps = [
        {"listing_key": "a", "street_address": "702 GREAT OAK LN", "sale_price": 800000, "list_price": 725000, "days_on_market": 5,
         "is_included": True, "commentary": "Comp #1 sold above list in 5 days.", "ai_commentary": "Comp #1 sold above list in 5 days."},
        {"listing_key": "b", "street_address": "710 WILLOWDALE LN", "sale_price": 550000, "list_price": 600000, "days_on_market": 55,
         "is_included": True, "commentary": "Comp #2 is the lowest-priced sale in the set. It sold as-is.",
         "ai_commentary": "Comp #2 is the lowest-priced sale in the set. It sold as-is."},
    ]
    strategy = ("**Pricing Position:** We recommend listing at about $665,000.\n\n"
                "**Competition:** Buyers will compare the home with Comp #1 and Comp #2, both of which sold above list within 10 days. "
                "Lead with the setting.\n\n**Overpricing Risk:** A home priced too high will not sell.")
    pricing = {"listing_strategy_notes": strategy, "recommended_list_price": 665000,
               "ai_estimate": {"value": 667000, "range_min": 615000, "range_max": 720000, "confidence": "medium",
                               "methodology_summary": "The final fair market value is $667,000, with a supported valuation range of $615,000-$720,000. The market is brisk.",
                               "listing_strategy_considerations": strategy, "qa_flags": ["earlier flag"],
                               "valuation_trace": {"anchor": {"comps": []}, "note": "Comp #1 and Comp #2, both of which sold above list within 10 days."}}}
    return SimpleNamespace(id="r1", mode="listing", subject={"street_address": "701 GREAT OAK LN", "annual_taxes": 9298},
                           comparables=comps, pricing=pricing)


def _finding(**k):
    return {"where": "strategy", "kind": "contradiction", "why": "Comp #2 sold 8.3% under asking after 55 days.", "action": "remove", **k}


def test_packet_holds_the_facts_and_the_text_and_no_engine_internals():
    r = _report()
    p = audit.build_packet(r.subject, r.comparables, r.pricing, r.mode)
    assert p["facts"]["report_for"].startswith("a seller")
    c2 = p["facts"]["comparable_sales"][1]
    assert c2["comp_number"] == 2 and c2["sold_vs_asking_pct"] == -8.3 and c2["days_on_market"] == 55
    assert set(p["text"]) == {"methodology", "strategy", "comp_note:1", "comp_note:2"}
    assert "valuation_trace" not in str(p) and "qa_flags" not in str(p)


def test_a_wrong_sentence_is_removed_from_every_copy_and_nothing_else_is_touched():
    r = _report()
    p = audit.build_packet(r.subject, r.comparables, r.pricing, r.mode)
    bad = "**Competition:** Buyers will compare the home with Comp #1 and Comp #2, both of which sold above list within 10 days."
    pricing, comps, applied, for_agent = audit.apply_findings(r.pricing, r.comparables, [_finding(sentence=bad)], p)
    for text in (pricing["listing_strategy_notes"], pricing["ai_estimate"]["listing_strategy_considerations"]):
        assert "both of which sold above list" not in text and "Lead with the setting." in text and "**Overpricing Risk:**" in text
    assert pricing["ai_estimate"]["value"] == 667000 and pricing["recommended_list_price"] == 665000
    assert "both of which sold above list" in pricing["ai_estimate"]["valuation_trace"]["note"]     # the audit trail is not edited
    assert len(applied) == 1 and not for_agent and comps == r.comparables


def test_the_value_sentence_can_never_be_removed_or_rewritten():
    r = _report()
    p = audit.build_packet(r.subject, r.comparables, r.pricing, r.mode)
    f = _finding(where="methodology", sentence="The final fair market value is $667,000, with a supported valuation range of $615,000-$720,000.")
    pricing, comps, applied, for_agent = audit.apply_findings(r.pricing, r.comparables, [f], p)
    assert not applied and len(for_agent) == 1 and pricing == r.pricing


def test_a_replacement_may_only_use_figures_already_on_the_page():
    r = _report()
    p = audit.build_packet(r.subject, r.comparables, r.pricing, r.mode)
    old = "Comp #2 is the lowest-priced sale in the set."
    ok = _finding(where="comp_note:2", sentence=old, action="replace", replacement="Comp #2 sold for $550,000 after 55 days.")
    pricing, comps, applied, for_agent = audit.apply_findings(r.pricing, r.comparables, [ok], p)
    assert comps[1]["commentary"] == "Comp #2 sold for $550,000 after 55 days. It sold as-is." and applied and not for_agent
    invented = _finding(where="comp_note:2", sentence=old, action="replace", replacement="Comp #2 sold for $512,345 after 41 days.")
    pricing, comps, applied, for_agent = audit.apply_findings(r.pricing, r.comparables, [invented], p)
    assert comps[1]["commentary"] == r.comparables[1]["commentary"] and not applied and len(for_agent) == 1


def test_omissions_misquotes_and_an_agents_own_note_go_to_the_agent():
    r = _report()
    r.comparables[0]["commentary"] = "My own words about this sale, sold above list in 5 days."     # the agent rewrote it
    p = audit.build_packet(r.subject, r.comparables, r.pricing, r.mode)
    findings = [
        {"where": "methodology", "sentence": "", "kind": "omission", "why": "The home's own 2025 sale is not mentioned.", "action": "flag"},
        _finding(sentence="A sentence the report does not contain."),
        _finding(where="comp_note:1", sentence="My own words about this sale, sold above list in 5 days."),
    ]
    pricing, comps, applied, for_agent = audit.apply_findings(r.pricing, r.comparables, findings, p)
    assert not applied and len(for_agent) == 3 and comps[0]["commentary"].startswith("My own words")
    msg = audit.agent_message(for_agent)
    assert msg.startswith("Review before sending: the final read of this report noted") and "2025 sale" in msg


def test_only_so_many_silent_edits(monkeypatch):
    r = _report()
    r.pricing["ai_estimate"]["methodology_summary"] = " ".join(f"Wrong sentence number {i}." for i in range(1, 10))
    p = audit.build_packet(r.subject, r.comparables, r.pricing, r.mode)
    findings = [_finding(where="methodology", sentence=f"Wrong sentence number {i}.") for i in range(1, 10)]
    pricing, comps, applied, for_agent = audit.apply_findings(r.pricing, r.comparables, findings, p)
    assert len(applied) == audit.MAX_TEXT_CHANGES and len(for_agent) == 9 - audit.MAX_TEXT_CHANGES


def _run(report, findings, monkeypatch, on=True, store=None):
    calls = {"model": 0, "usage": [], "spend": []}
    store = {} if store is None else store
    monkeypatch.setenv("CMA_FINAL_AUDIT", "1" if on else "0")

    async def create(client, **kw):
        calls["model"] += 1
        assert kw["tool_choice"] == {"type": "tool", "name": "submit_cma_audit"} and "FACTS:" in kw["messages"][0]["content"]
        block = SimpleNamespace(type="tool_use", name="submit_cma_audit", input={"findings": findings})
        return SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=9000, output_tokens=400))

    async def record(db, owner_id, _a, model, tin, tout, cost, *_r, event_type=None):
        calls["usage"].append((event_type, tin, tout, cost))

    async def add_spend(db, buckets, cost):
        calls["spend"].append((tuple(buckets), cost))

    async def lock_get(db, rid, kind, fp, order=None):
        return store.get((rid, kind, fp))

    async def lock_put(db, rid, kind, fp, raw, order=None, **more):
        store[(rid, kind, fp)] = {"fp": fp, "raw": raw}

    out = asyncio.run(audit.run(None, report, owner_id="o1", config={}, model="claude-sonnet-5-5", client=object(), create=create,
                                record_usage_event=record, add_spend=add_spend, estimate_cost=lambda c, m, i, o: 0.12,
                                lock_get=lock_get, lock_put=lock_put))
    return out, calls, store


def test_off_by_default_and_never_called(monkeypatch):
    r = _report()
    before = r.pricing
    out, calls, _ = _run(r, [], monkeypatch, on=False)
    assert out is None and calls["model"] == 0 and r.pricing is before


def test_a_run_edits_the_text_bills_once_and_keeps_the_value(monkeypatch):
    r = _report()
    bad = "**Competition:** Buyers will compare the home with Comp #1 and Comp #2, both of which sold above list within 10 days."
    findings = [_finding(sentence=bad),
                {"where": "methodology", "sentence": "", "kind": "omission", "why": "Taxes of $9,298 are not mentioned.", "action": "flag"}]
    out, calls, store = _run(r, findings, monkeypatch)
    est = r.pricing["ai_estimate"]
    assert est["value"] == 667000 and est["range_min"] == 615000 and r.pricing["recommended_list_price"] == 665000
    assert "both of which sold above list" not in r.pricing["listing_strategy_notes"]
    assert est["audit_review"]["message"].startswith("Review before sending") and len(est["final_audit"]["applied"]) == 1
    assert any(f.startswith("Final read: removed a sentence in strategy") for f in est["qa_flags"]) and "earlier flag" in est["qa_flags"]
    assert calls["model"] == 1 and calls["usage"] == [("cma_audit", 9000, 400, 0.12)] and calls["spend"][0][1] == 0.12
    assert out["cost_usd"] == 0.12 and out["reused"] is False


def test_the_same_report_again_is_free(monkeypatch):
    r1, r2 = _report(), _report()
    _, calls1, store = _run(r1, [], monkeypatch)
    out2, calls2, _ = _run(r2, [], monkeypatch, store=store)
    assert calls1["model"] == 1 and calls2["model"] == 0 and calls2["usage"] == [] and out2["reused"] is True and out2["cost_usd"] == 0.0


def test_a_failure_leaves_the_report_alone(monkeypatch):
    r = _report()
    before = r.pricing
    monkeypatch.setenv("CMA_FINAL_AUDIT", "1")

    async def boom(client, **kw):
        raise RuntimeError("provider down")

    async def none(*a, **k):
        return None
    out = asyncio.run(audit.run(None, r, owner_id="o1", config={}, model="m", client=object(), create=boom, record_usage_event=none,
                                add_spend=none, estimate_cost=lambda *a: 0.0, lock_get=none, lock_put=none))
    assert out is None and r.pricing is before
