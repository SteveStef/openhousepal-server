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


# ---- v2 (first live batch, 11/14): not finding a detail is not the detail being wrong ---------------------------

def _mini():
    pricing = {"listing_strategy_notes": "**Pricing Position:** The current $469,900 asking price is about 2% above the "
               "supported value of $460,000. We recommend repositioning to about $460,000.\n\n**Overpricing Risk:** "
               "After 61 days on the market without an accepted offer, buyers have answered that price. The home shows well.",
               "ai_estimate": {"value": 460000, "methodology_summary": "It adds a deck and a wooded view. Possession is "
                               "listed as negotiable. The two larger sales carry little weight."}}
    comps = [{"street_address": "1 A ST", "commentary": "It adds a deck and tree view. It sold in 3 days.", "ai_commentary":
              "It adds a deck and tree view. It sold in 3 days."}]
    packet = audit.build_packet({"street_address": "9 Z ST"}, comps, pricing, "listing")
    return pricing, comps, packet


def test_a_detail_the_audit_could_not_find_is_never_removed_from_the_text():
    pricing, comps, packet = _mini()
    findings = [
        {"where": "comp_note:1", "sentence": "It adds a deck and tree view.", "kind": "unsupported",
         "why": "No deck or view appears in the records.", "action": "remove"},
        {"where": "methodology", "sentence": "Possession is listed as negotiable.", "kind": "unsupported",
         "why": "No possession terms appear in the facts.", "action": "remove"},
    ]
    new_pricing, new_comps, applied, for_agent = audit.apply_findings(pricing, comps, findings, packet)
    assert applied == [] and for_agent == []
    assert new_comps[0]["commentary"] == comps[0]["commentary"]
    assert "Possession is listed as negotiable." in new_pricing["ai_estimate"]["methodology_summary"]
    # listed for the agent to check - except what a photo shows (the deck; 11/15), which the writers saw
    assert [u["sentence"] for u in audit.unverified_findings(findings)] == ["Possession is listed as negotiable."]


def test_a_finding_that_talks_itself_out_of_the_error_is_dropped():
    pricing, comps, packet = _mini()
    findings = [
        {"where": "comp_note:1", "sentence": "It sold in 3 days.", "kind": "contradiction",
         "why": "The record shows 3 days on market, so this is true; no conflict.", "action": "flag"},
        {"where": "methodology", "sentence": "The two larger sales carry little weight.", "kind": "contradiction",
         "why": "Comp 3 has a 24.7% share, one of the largest.", "action": "remove"},
    ]
    new_pricing, _, applied, for_agent = audit.apply_findings(pricing, comps, findings, packet)
    assert for_agent == []
    assert [a["sentence"] for a in applied] == ["The two larger sales carry little weight."]
    assert "little weight" not in new_pricing["ai_estimate"]["methodology_summary"]


def test_speculation_is_removed_but_the_computed_pricing_position_is_only_flagged():
    pricing, comps, packet = _mini()
    findings = [
        {"where": "strategy", "sentence": "After 61 days on the market without an accepted offer, buyers have answered that price.",
         "kind": "speculation", "why": "States how buyers reacted with nothing behind it.", "action": "remove"},
        {"where": "strategy", "sentence": "We recommend repositioning to about $460,000.", "kind": "inconsistent",
         "why": "Disagrees with another section.", "action": "remove"},
    ]
    new_pricing, _, applied, for_agent = audit.apply_findings(pricing, comps, findings, packet)
    assert [a["sentence"] for a in applied] == [findings[0]["sentence"]]
    assert "buyers have answered" not in new_pricing["listing_strategy_notes"]
    assert "We recommend repositioning to about $460,000." in new_pricing["listing_strategy_notes"]
    assert [n["sentence"] for n in for_agent] == ["We recommend repositioning to about $460,000."]


def test_the_packet_carries_each_homes_own_record_and_listing_text():
    comps = [{"street_address": "1 A ST", "listing_key": "k1", "public_remarks": "Deck off the kitchen with a wooded view.",
              "heating": ["Heat Pump"], "sewer": ["On Site Septic"], "school_district": "COATESVILLE AREA",
              "architectural_style": ["Ranch/Rambler"], "photos": ["http://x/1.jpg"], "latitude": 40.1}]
    packet = audit.build_packet({"street_address": "9 Z ST", "public_remarks": "Possession negotiable.", "cooling": ["Central A/C"]},
                                comps, {"ai_estimate": {"value": 1}}, "buyer")
    c = packet["facts"]["comparable_sales"][0]
    assert c["listing_text"].startswith("Deck off the kitchen") and c["heating"] == ["Heat Pump"]
    assert c["school_district"] == "COATESVILLE AREA" and c["architectural_style"] == ["Ranch/Rambler"]
    assert "photos" not in c and "latitude" not in c
    assert packet["facts"]["subject"]["listing_text"] == "Possession negotiable."


def test_the_engines_own_computed_lines_are_neither_edited_nor_flagged():
    pricing, comps, packet = _mini()
    findings = [
        {"where": "methodology", "sentence": "The value is therefore not set above about $583,000.", "kind": "inconsistent",
         "why": "The adjusted sales figure of $593,000 less $13,000 gives $580,000, not $583,000.", "action": "flag"},
        {"where": "comp_note:1", "sentence": "Its annual property taxes are $6,596, against $8,735 for this home - a gap that "
         "size usually means a tax abatement or an old assessment.", "kind": "speculation",
         "why": "The reason for the tax gap is asserted without any supporting fact.", "action": "remove"},
        {"where": "comp_note:1", "sentence": "It sold in 3 days.", "kind": "inconsistent",
         "why": "Fine as 3 days, flagged only lightly.", "action": "flag"},
    ]
    _, _, applied, for_agent = audit.apply_findings(pricing, comps, findings, packet)
    assert applied == [] and for_agent == []


# ---- outside audit of round 5 (11/14 night): a fact the listing or the MLS record states is verified ---------------

def test_the_whole_listing_description_reaches_the_last_reader():
    # 18 Colts Neck Dr: "all new Anderson windows & doors ... updated HVAC system" sits 2,244 characters in. The
    # text was cut at 1,800, so the reader listed the home's new windows as something it "could not verify".
    long_text = ("Welcome home. " * 160) + "All new Anderson windows & doors throughout and an updated HVAC system."
    assert len(long_text) > 2200
    packet = audit.build_packet({"street_address": "18 COLTS NECK DR", "public_remarks": long_text}, [],
                                {"ai_estimate": {"value": 1}}, "buyer")
    assert "Anderson windows" in packet["facts"]["subject"]["listing_text"]
    assert "updated HVAC system" in packet["facts"]["subject"]["listing_text"]


def test_each_homes_own_mls_record_reaches_the_last_reader():
    # 101 Sues Way "the water heater is electric", 404 Mountainview Dr "possession is negotiable ... natural gas hot
    # water ... a gas fireplace": every one is a field of the home's Bright record, which the saved report does not
    # keep. The caller passes the records in; names, phone numbers and photo links stay out.
    records = {
        "s1": {"HotWater": ["Natural Gas"], "Possession": ["Negotiable"], "FireplaceFeatures": ["Gas/Propane"],
               "Flooring": ["Carpet", "Wood"], "ListAgentFullName": "Some Agent", "ListAgentEmail": "a@example.com",
               "ListPictureURL": "http://x/1.jpg", "ListingTaxID": "43-05", "PublicRemarks": "Top-floor unit."},
        "c1": {"HotWater": ["Electric"], "Heating": ["Baseboard - Hot Water"], "ListOfficePhone": "5550100",
               "PublicRemarks": "A much longer description than the saved one, ending with a home gym."},
    }
    comps = [{"street_address": "1 A ST", "listing_key": "c1", "public_remarks": "Short."}]
    packet = audit.build_packet({"street_address": "404 M DR", "listing_key": "s1"}, comps, {"ai_estimate": {"value": 1}},
                                "buyer", records=records)
    subj, comp = packet["facts"]["subject"], packet["facts"]["comparable_sales"][0]
    assert subj["mls_record"] == {"FireplaceFeatures": ["Gas/Propane"], "Flooring": ["Carpet", "Wood"],
                                  "HotWater": ["Natural Gas"], "Possession": ["Negotiable"]}
    assert subj["listing_text"] == "Top-floor unit."                      # a snapshot with no description gets the record's
    assert comp["mls_record"]["HotWater"] == ["Electric"] and "ListOfficePhone" not in comp["mls_record"]
    assert comp["listing_text"].endswith("home gym.")                     # ...and a shortened one gets the full text
    flat = str(packet)
    assert "Some Agent" not in flat and "a@example.com" not in flat and "http://x/1.jpg" not in flat and "43-05" not in flat


def test_no_records_passed_changes_nothing():
    comps = [{"street_address": "1 A ST", "listing_key": "c1", "public_remarks": "Short."}]
    a = audit.build_packet({"street_address": "9 Z ST", "listing_key": "s1"}, comps, {"ai_estimate": {"value": 1}}, "buyer")
    b = audit.build_packet({"street_address": "9 Z ST", "listing_key": "s1"}, comps, {"ai_estimate": {"value": 1}}, "buyer", records={})
    assert a == b and "mls_record" not in a["facts"]["subject"]


def test_the_reader_is_told_where_verified_facts_live():
    assert "listing_text" in audit.SYSTEM and "mls_record" in audit.SYSTEM and "balcony" in audit.SYSTEM


# ---- outside audits of rounds 5 and 6: a detail found in neither the listing nor the record is put to the agent ------

def test_details_it_could_not_find_are_listed_for_the_agent_to_check():
    unverified = [{"where": "inspection", "kind": "unsupported", "sentence": "Verify the condition of the carport roof covering.",
                   "why": "The subject has a detached garage and no carport in the facts."},
                  {"where": "comp_note:3", "kind": "unsupported", "sentence": "It has a fully renovated kitchen with quartz counters.",
                   "why": "Listing says 'gorgeous counters', not quartz."}]
    msg = audit.agent_message([], unverified)
    assert msg.startswith("Review before sending: these details are in neither the listing text nor the MLS record")
    assert "check each against the photos" in msg and "carport roof covering" in msg and "quartz counters" in msg
    both = audit.agent_message([{"why": "Comp 3 carries a 24.7% share.", "sentence": "It carries little weight."}], unverified)
    assert 'the final read of this report noted - (1) "It carries little weight." - Comp 3 carries a 24.7% share.' in both \
        and "These details are in neither" in both
    assert audit.agent_message([], []) is None and audit.agent_message([]) is None
    many = audit.agent_message([], [dict(unverified[0], sentence=f"Sentence {i}.") for i in range(6)])
    assert "(+3 more)" in many and "Sentence 2." in many and "Sentence 3." not in many


# ---- 11/15: part of the main flow (owner: "as long as it does not add a confusing step for the agent and it just comes
# out as a finished report") - on by default, and what it leaves for the agent is short and worth reading ---------------

def test_the_final_read_is_on_unless_switched_off(monkeypatch):
    monkeypatch.delenv("CMA_FINAL_AUDIT", raising=False)
    assert audit.enabled() is True
    monkeypatch.setenv("CMA_FINAL_AUDIT", "0")
    assert audit.enabled() is False
    monkeypatch.setenv("CMA_FINAL_AUDIT", "1")
    assert audit.enabled() is True


def test_a_note_that_talks_itself_down_never_reaches_the_agent():
    r = _report()
    p = audit.build_packet(r.subject, r.comparables, r.pricing, r.mode)
    weak = ["Minor; lot 34,412 sqft equals 0.79 acres, so this is consistent.",
            "Comp 1 is a 4-bed twin; $310,000/2,203 is about $141, fine, but the 'brick, similar exterior' is mixed siding; main issue none.",
            "Hedged wording, so it is not a claim of fact. Flagged only as low-risk.",
            "Garbled phrase; comp 3 is farthest comp but fine otherwise.",
            "Comp 3 has 16.7% share, but 'moderate' is fine; unverified.",
            "Predicting the outcome is stated as likely fact beyond the figures; mild, but the claim rests on no figure.",
            "Weak; consistent with facts.", "Hedged, but speculates about seller motive with no supporting fact."]
    real = "The sale is over a year older than the others, yet the methodology calls it one of three nearby sales used."
    findings = [_finding(where="methodology", sentence="", kind="omission", action="flag", why=w) for w in weak + [real]]
    _, _, applied, for_agent = audit.apply_findings(r.pricing, r.comparables, findings, p)
    assert applied == [] and [n["why"] for n in for_agent] == [real]


def test_a_real_fix_is_never_dropped_for_its_wording():
    """The weak-note words judge only what is LEFT for the agent - a sentence the read fixes is still fixed."""
    r = _report()
    p = audit.build_packet(r.subject, r.comparables, r.pricing, r.mode)
    f = _finding(where="comp_note:2", sentence="Comp #2 is the lowest-priced sale in the set.", why="A minor slip, but Comp #2 is not the lowest sale.")
    _, comps, applied, for_agent = audit.apply_findings(r.pricing, r.comparables, [f], p)
    assert len(applied) == 1 and for_agent == [] and "lowest-priced" not in comps[1]["ai_commentary"]


def test_the_agent_is_not_asked_to_verify_what_a_photo_shows():
    found = audit.unverified_findings([
        {"kind": "unsupported", "sentence": "It has a fully renovated kitchen with quartz counters.", "why": "Listing says 'gorgeous counters', not quartz."},
        {"kind": "unsupported", "sentence": "Its oak-cabinet kitchen and carpeted lower level are dated.", "why": "Comp 1 facts describe solid cabinetry and no oak cabinets or carpeted lower level."},
        {"kind": "unsupported", "sentence": "Its kitchen finishes are dated beside the subject's.", "why": "Nothing in the facts says its kitchen finishes are dated."},
        {"kind": "unsupported", "sentence": "Verify the condition of the carport roof covering.", "why": "The subject has a detached garage and no carport in the facts."},
        {"kind": "unsupported", "sentence": "Central air is documented as updated.", "why": "Subject facts do not say the central air was updated; only that central A/C exists."},
        {"kind": "unsupported", "sentence": "It is a turnkey, occupied home.", "why": "Occupancy is not stated anywhere in comp 5's facts."},
    ])
    assert [u["sentence"] for u in found] == ["Verify the condition of the carport roof covering.", "Central air is documented as updated.",
                                              "It is a turnkey, occupied home."]


def test_the_judgment_steps_own_line_is_left_alone():
    r = _report()
    line = "A judgment, not a measured adjustment: carpet over hardwood in the bedrooms."
    r.pricing["ai_estimate"]["methodology_summary"] += " " + line
    p = audit.build_packet(r.subject, r.comparables, r.pricing, r.mode)
    f = _finding(where="methodology", sentence=line, action="replace", replacement="A judgment, not a measured adjustment: judgment beyond the measured adjustments.",
                 why="The facts give no carpet-over-hardwood reason for it.")
    pricing, _, applied, for_agent = audit.apply_findings(r.pricing, r.comparables, [f], p)
    assert applied == [] and for_agent == [] and line in pricing["ai_estimate"]["methodology_summary"]


def test_a_guard_that_objects_skips_the_read_and_costs_nothing(monkeypatch):
    """Accuracy-test time machine: the packet is scanned for the hidden sale facts before it is sent."""
    monkeypatch.setenv("CMA_FINAL_AUDIT", "1")
    r = _report()
    before = r.pricing
    called = {"n": 0}

    async def create(client, **kw):
        called["n"] += 1

    async def nothing(*a, **k):
        return None

    def guard(packet):
        assert packet["text"] and packet["facts"]
        raise RuntimeError("leak")

    out = asyncio.run(audit.run(None, r, owner_id="o1", config={}, model="m", client=object(), create=create, record_usage_event=nothing,
                                add_spend=nothing, estimate_cost=lambda *a: 0.0, lock_get=nothing, lock_put=nothing, guard=guard))
    assert out is None and called["n"] == 0 and r.pricing is before
