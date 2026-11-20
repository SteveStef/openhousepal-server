"""Seller CMA two paths (owner 11/19): the AI reads the photos first and suggests the Compare step's answers; the
report reuses that read. Free tests - no model is called (a fake client stands in where one would be)."""
import asyncio
import json
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import app.main  # noqa: F401  (import order, like the other tests)
from app.services import cma_ai_service as ai
from app.services import cma_seller_prefill as pf


def _comp(k, **kw):
    return {"listing_key": k, "street_address": f"{k} MAIN ST", "is_included": True, **kw}


# ---- the AI's comp rating -> the agent's answer -------------------------------------------------------------------
def test_mark_from_quality():
    assert pf.mark_from_quality({"finish_quality": "above", "interior": "above"}) == "worse"     # comp nicer
    assert pf.mark_from_quality({"finish_quality": "below", "interior": "below"}) == "better"
    assert pf.mark_from_quality({"finish_quality": "similar", "interior": "similar"}) == "same"
    assert pf.mark_from_quality({"finish_quality": "above", "interior": "below"}) == "same"      # mixed = same
    assert pf.mark_from_quality({"finish_quality": "above"}) == "worse"                          # one row is enough
    assert pf.mark_from_quality({"kitchen": "above"}) is None                                    # kitchen alone: no
    assert pf.mark_from_quality({}) is None and pf.mark_from_quality(None) is None


# ---- all photos / some photos / none ------------------------------------------------------------------------------
def test_all_photos_fill_everything():
    comps = [_comp("A"), _comp("B"), _comp("C")]
    result = {"comps": [{"listing_key": "A", "quality": {"finish_quality": "above", "interior": "above"}},
                        {"listing_key": "B", "quality": {"finish_quality": "similar", "interior": "similar"}},
                        {"listing_key": "C", "quality": {"finish_quality": "below", "interior": "below"}}],
              "subject_rooms": {"kitchen": "updated", "baths": "original"}}
    s = pf.suggestions(result, ["exterior_front", "kitchen", "bathroom"], comps)
    assert s["comp_marks"] == {"A": "worse", "B": "same", "C": "better"}
    assert s["unread"] == [] and s["kitchen"] == "updated" and s["baths"] == "original" and s["rooms_missing"] == []


def test_no_kitchen_photo_asks_the_kitchen():
    comps = [_comp("A"), _comp("B")]
    result = {"comps": [{"listing_key": "A", "quality": {"finish_quality": "above"}},
                        {"listing_key": "B", "quality": {"interior": "similar"}}],
              # the AI guessed a kitchen it was never shown: the photo sort says no kitchen photo - not used
              "subject_rooms": {"kitchen": "original", "baths": "redone"}}
    s = pf.suggestions(result, ["exterior_front", "bathroom", "bedroom"], comps)
    assert s["kitchen"] is None and s["baths"] == "redone" and s["rooms_missing"] == ["kitchen"]


def test_room_the_ai_says_is_not_shown_is_asked():
    s = pf.suggestions({"comps": [], "subject_rooms": {"kitchen": "not_shown", "baths": "not_shown"}}, None, [])
    assert s["kitchen"] is None and s["baths"] is None and s["rooms_missing"] == ["kitchen", "baths"]


def test_comp_the_ai_could_not_judge_is_asked():
    comps = [_comp("A"), _comp("B"), {"id": "manual-1", "street_address": "9 HAND ST", "is_included": True},
             _comp("X", is_included=False)]
    result = {"comps": [{"listing_key": "A", "quality": {"finish_quality": "above"}},
                        {"listing_key": "B", "quality": {}}]}
    s = pf.suggestions(result, None, comps)
    assert s["comp_marks"] == {"A": "worse"}
    assert s["unread"] == ["B", "manual-1"]            # left-out comp X is not asked about


def test_no_read_at_all():
    s = pf.suggestions({}, None, [_comp("A")])
    assert s == {"comp_marks": {}, "unread": ["A"], "kitchen": None, "baths": None, "rooms_missing": ["kitchen", "baths"]}


# ---- state: stale when the comps or photos changed; a read cut off by a restart ------------------------------------
def _report(comps, subject=None):
    return SimpleNamespace(comparables=comps, subject=subject or {"photos": ["p1", "p2"]})


def test_state_stale_when_comps_change():
    rep = _report([_comp("A"), _comp("B")])
    st = {"status": "ready", "comp_keys": pf.included_comp_keys(rep.comparables), "photos": ai._seller_photos_sig(rep.subject)}
    assert pf.public_state(rep, st)["stale"] is False
    rep2 = _report([_comp("A"), _comp("C")], rep.subject)
    assert pf.public_state(rep2, st)["stale"] is True


def test_state_stale_when_photos_change():
    rep = _report([_comp("A"), _comp("B")])
    st = {"status": "ready", "comp_keys": pf.included_comp_keys(rep.comparables), "photos": ai._seller_photos_sig(rep.subject)}
    rep2 = _report(rep.comparables, {"photos": ["p1", "p2"], "condition_photos": ["mine1"]})
    assert pf.public_state(rep2, st)["stale"] is True


def test_reading_cut_off_by_a_restart_becomes_failed():
    old = (datetime.now(timezone.utc) - timedelta(minutes=pf.STALE_READ_MINUTES + 5)).isoformat()
    st = pf.public_state(_report([]), {"status": "reading", "started_at": old})
    assert st["status"] == "failed" and "Try again" in st["error"]
    fresh = pf.public_state(_report([]), {"status": "reading", "started_at": datetime.now(timezone.utc).isoformat()})
    assert fresh["status"] == "reading"
    # a long Save usage read keeps beating: still reading after 20 minutes
    long = pf.public_state(_report([]), {"status": "reading", "started_at": old, "beat_at": datetime.now(timezone.utc).isoformat()})
    assert long["status"] == "reading"
    assert pf.public_state(_report([]), None) == {"status": "none"}


# ---- the report's tool: kitchen / baths read for a seller report only (buyer reports unchanged) -------------------
def test_subject_rooms_only_on_seller_tool():
    seller = ai._tool_for_mode(ai._SUBMIT_TOOL, "listing")
    buyer = ai._tool_for_mode(ai._SUBMIT_TOOL, "buyer")
    assert "subject_rooms" in seller["input_schema"]["properties"]
    assert "subject_rooms" not in buyer["input_schema"]["properties"]
    assert "subject_rooms" not in ai._SUBMIT_TOOL["input_schema"]["properties"]        # shared schema untouched
    # always answered on a seller report ('not_shown' when the photos don't show the room); never on a buyer report
    assert "subject_rooms" in seller["input_schema"]["required"] and seller["input_schema"]["properties"]["subject_rooms"]["required"] == ["kitchen", "baths"]
    assert "subject_rooms" not in buyer["input_schema"].get("required", [])
    assert "subject_rooms" not in ai._SUBMIT_TOOL["input_schema"].get("required", [])
    est = ai._tool_for_mode(ai._ESTIMATE_ONLY_TOOL, "listing")
    assert "subject_rooms" not in est["input_schema"]["properties"]


# ---- agent overrode the AI: only that comp's note is rewritten, text only -----------------------------------------
class _FakeMessages:
    def __init__(self, reply=None, boom=False):
        self.calls, self.reply, self.boom = [], reply, boom

    async def create(self, **kw):
        self.calls.append(kw)
        if self.boom:
            raise RuntimeError("provider down")
        text = kw["messages"][0]["content"]
        import re
        keys = re.findall(r"^\[([^\]]+)\]", text, re.M)
        body = self.reply if self.reply is not None else {"notes": [{"key": k, "text": f"new note {k}"} for k in keys]}
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(body))],
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def _run_rewrite(monkeypatch, fake, result, marks):
    monkeypatch.setattr(ai, "_get_client", lambda: SimpleNamespace(messages=fake))

    async def _create(client, **kw):
        return await client.messages.create(**kw)
    monkeypatch.setattr(ai._cma_claude, "create", _create)
    return asyncio.run(ai._rewrite_overridden_comp_notes("m", result, marks, [_comp("A"), _comp("B"), _comp("C")]))


def _result():
    return {"comps": [{"listing_key": "A", "note": "A has nicer finishes.", "quality": {"finish_quality": "above", "interior": "above"}},
                      {"listing_key": "B", "note": "B is similar.", "quality": {"finish_quality": "similar", "interior": "similar"}},
                      {"listing_key": "C", "note": "C is dated.", "quality": {}}]}


def test_rewrite_only_the_overridden_comp(monkeypatch):
    fake = _FakeMessages()
    res = _result()
    # agent agrees on B (same), overrides A (AI: comp nicer -> agent: this home nicer), C had no AI read
    n = _run_rewrite(monkeypatch, fake, res, {"A": "better", "B": "same", "C": "worse"})
    assert n == 1 and len(fake.calls) == 1
    assert "[A]" in fake.calls[0]["messages"][0]["content"] and "[B]" not in fake.calls[0]["messages"][0]["content"]
    assert "images" not in json.dumps(fake.calls[0]["messages"])          # text only - no photos sent again
    assert res["comps"][0]["note"] == "new note A" and res["comps"][1]["note"] == "B is similar."


def test_no_override_no_call(monkeypatch):
    fake = _FakeMessages()
    assert _run_rewrite(monkeypatch, fake, _result(), {"A": "worse", "B": "same"}) == 0
    assert fake.calls == []


def test_not_sure_is_not_an_override(monkeypatch):
    fake = _FakeMessages()
    assert _run_rewrite(monkeypatch, fake, _result(), {"A": "not_sure"}) == 0 and fake.calls == []


def test_rewrite_failure_keeps_the_note(monkeypatch):
    fake = _FakeMessages(boom=True)
    res = _result()
    assert _run_rewrite(monkeypatch, fake, res, {"A": "better"}) == 0
    assert res["comps"][0]["note"] == "A has nicer finishes."


def test_rewrite_missing_entry_keeps_that_note(monkeypatch):
    fake = _FakeMessages(reply={"notes": []})
    res = _result()
    assert _run_rewrite(monkeypatch, fake, res, {"A": "better"}) == 0
    assert res["comps"][0]["note"] == "A has nicer finishes."


# ---- the stored read: same comps + same photos = reused; anything changed = not -----------------------------------
def test_seller_read_reuse_rules(monkeypatch):
    store = {}

    async def _get(db, key):
        return store.get(key)

    async def _put(db, key, raw):
        store[key] = raw
    monkeypatch.setattr(ai, "_reasoning_cache_get", _get)
    monkeypatch.setattr(ai, "_reasoning_cache_put", _put)
    raw = {"comps": [{"listing_key": "A", "note": "Comp #1 is close."}]}
    asyncio.run(ai._seller_read_put(None, "r1", "fp1", raw, ["A", "B"], "ph1"))
    assert asyncio.run(ai._seller_read_get(None, "r1", "fp1", ["A", "B"], "ph1"))["raw"] == raw
    assert asyncio.run(ai._seller_read_get(None, "r1", "fp2", ["A", "B"], "ph1")) is None     # comps / facts changed
    assert asyncio.run(ai._seller_read_get(None, "r1", "fp1", ["A", "B"], "ph2")) is None     # photos changed
    assert asyncio.run(ai._seller_read_get(None, "r2", "fp1", ["A", "B"], "ph1")) is None     # another report
    # same comps in another order: "Comp #N" renumbered, still reused
    got = asyncio.run(ai._seller_read_get(None, "r1", "fp1", ["B", "A"], "ph1"))
    assert got and "Comp #2" in json.dumps(got["raw"])


def test_photos_sig():
    a = ai._seller_photos_sig({"photos": ["1", "2"]})
    assert a == ai._seller_photos_sig({"photos": ["1", "2", None], "seller_answers": {"kitchen": "redone"}})   # answers don't count
    assert a != ai._seller_photos_sig({"photos": ["1", "2"], "condition_photos": ["mine"]})
    assert a != ai._seller_photos_sig({"photos": ["2", "1"]})


# ---- a seller home typed in by hand: the six core facts are required -------------------------------------------
def test_core_facts_required_for_manual_seller_home_only():
    blank = {"source": "manual", "latitude": 40.0, "longitude": -75.0}
    miss = pf.core_facts_missing(blank, "listing")
    assert miss == ["property type", "bedrooms", "bathrooms", "above-grade square feet", "year built"]
    full = {**blank, "property_type": "SINGLE_FAMILY", "beds": 3, "baths": 2, "sqft": 1800, "year_built": 1990}
    assert pf.core_facts_missing(full, "listing") == []
    assert pf.core_facts_missing({**full, "beds": 0}, "listing") == []           # 0 beds (a studio) is a fact
    assert pf.core_facts_missing(blank, "buyer") == []                            # buyer reports unchanged
    assert pf.core_facts_missing({**blank, "listing_key": "123"}, "listing") == []  # MLS home: the record has them
    assert pf.core_facts_missing({"property_type": "X"}, "listing") == []          # not hand-entered
    assert "bedrooms" in pf.core_facts_message(["bedrooms"])


# ---- live test 11/19: the agent's evidence note names the method this report really used -------------------------
def test_evidence_note_names_comp_answers_on_answered_seller_report():
    rows = [{"listing_key": str(i), "normalized_weight": 0.2, "gross_adjustment_pct": 5} for i in range(5)]
    comps = [{"listing_key": str(i), "match_score": 90} for i in range(5)]
    plain = {"anchor": {"comps": rows}}
    old = ai._evidence_strength({}, plain, 700000, comps)
    assert old["level"] == "fair" and "adjustment table alone, half landed" in old["message"]
    placed = {**plain, "placement_check": {"how": "comp_marks"}}
    new = ai._evidence_strength({}, placed, 700000, comps)
    assert new["level"] == "fair" and "comp-by-comp answers" in new["message"]
    assert "as it is today" in new["message"]


# ---- one-pass seller report (owner 11/19, "stupid simple") --------------------------------------------------------
def _read3():
    return {"comps": [{"listing_key": "A", "quality": {"finish_quality": "above", "interior": "above"}},
                      {"listing_key": "B", "quality": {"finish_quality": "similar", "interior": "similar"}},
                      {"listing_key": "C", "quality": {}}],
            "subject_rooms": {"kitchen": "redone", "baths": "original"}}


def test_fill_blanks_only_when_photos_are_current():
    comps = [_comp("A"), _comp("B"), _comp("C")]
    assert pf.fill_blanks({"seller_answers": {}}, _read3(), None, comps) is None                 # question not answered
    assert pf.fill_blanks({"seller_answers": {"inside_photos": "no"}}, _read3(), None, comps) is None
    out = pf.fill_blanks({"x": 1, "seller_answers": {"inside_photos": "yes"}}, _read3(), None, comps)
    sa = out["seller_answers"]
    assert out["x"] == 1
    assert sa["comp_marks"] == {"A": "worse", "B": "same"}
    assert sa["kitchen"] == "redone" and sa["baths"] == "original"
    assert sa["ai_filled"] == ["A", "B", "baths", "kitchen"]
    assert sa["ai_unread"] == ["C"] and sa["ai_rooms_missing"] == []


def test_fill_blanks_never_touches_the_agents_answers():
    comps = [_comp("A"), _comp("B"), _comp("C")]
    sub = {"seller_answers": {"inside_photos": "yes", "comp_marks": {"A": "better"}, "kitchen": "updated"}}
    sa = pf.fill_blanks(sub, _read3(), ["kitchen"], comps)["seller_answers"]          # bathrooms not shown
    assert sa["comp_marks"] == {"A": "better", "B": "same"}                              # A stays the agent's
    assert sa["kitchen"] == "updated" and "baths" not in sa                               # no bath photo: not filled
    assert sa["ai_filled"] == ["B"] and sa["ai_rooms_missing"] == ["baths"]


def test_answers_from_photos_are_credited_to_the_photos():
    comps = [_comp("A", street_address="1 OAK LN"), _comp("B", street_address="2 ELM ST")]
    sub = {"seller_answers": {"inside_photos": "yes", "comp_marks": {"A": "worse", "B": "better"},
                              "kitchen": "redone", "ai_filled": ["A", "kitchen"]}}
    t = ai._agent_walkthrough_text(sub, for_prompt=True, comps=comps)
    agent_part, photo_part = t.split("Read from the home's photos")
    assert "nicer than 2 Elm St" in agent_part and "1 Oak" not in agent_part and "Kitchen" not in agent_part
    assert "not as nice as 1 Oak Ln" in photo_part and "Kitchen: " in photo_part
    assert "never to the agent" in photo_part
    only_ai = {"seller_answers": {"inside_photos": "yes", "comp_marks": {"A": "worse"}, "ai_filled": ["A"]}}
    assert "Given by the agent" not in ai._agent_walkthrough_text(only_ai, comps=comps)


def test_placement_note_says_photos_when_every_answer_is_the_ais():
    anchor = {"comps": [{"listing_key": k, "normalized_weight": 0.25, "size_adjusted_price": p, "status": "sold"}
                        for k, p in (("A", 500000), ("B", 520000), ("C", 540000), ("D", 560000))]}
    marks = {"A": "better", "B": "same", "C": "same", "D": "worse"}
    ai_pc = ai._seller_placement_check({"seller_answers": {"comp_marks": marks, "ai_filled": list(marks)}}, anchor, "listing")
    me_pc = ai._seller_placement_check({"seller_answers": {"comp_marks": marks}}, anchor, "listing")
    assert ai_pc["note"].startswith("Compared from its photos") and me_pc["note"].startswith("The agent who has walked")
    assert ai_pc["target"] == me_pc["target"]                     # same answers, same value - only the credit differs


def test_saved_value_notices_a_changed_answer_but_buyer_fingerprint_is_unchanged():
    pkt = {"subject": {"address": "1 Main"}, "comparables": [], "report_mode": "buyer"}
    assert "place" not in ai._packet_lock_identity(pkt)           # buyer reports: same fingerprint as before
    a = ai._packet_lock_identity({**pkt, "report_mode": "listing", "placement_check": {"how": "comp_marks", "target": 500000}})
    b = ai._packet_lock_identity({**pkt, "report_mode": "listing", "placement_check": {"how": "comp_marks", "target": 520000}})
    assert a != b


def test_update_run_reuses_the_read_when_only_comp_facts_moved(monkeypatch):
    stored = {"fp": "old", "photos": "P", "raw": {"comps": [{"listing_key": "A"}, {"listing_key": "B"}]}, "order": ["A", "B"]}

    async def fake_get(db, key):
        return stored
    monkeypatch.setattr(ai, "_reasoning_cache_get", fake_get)
    run = lambda **kw: asyncio.run(ai._seller_read_get(None, "r1", "new", ["B", "A"], kw.get("photos", "P"), loose=kw.get("loose", False)))
    assert run() is None                                          # a normal run: facts changed -> no reuse
    assert run(loose=True) is not None                            # Update the value: same comps, same photos
    assert run(loose=True, photos="Q") is None                    # new photos: never reused
    stored["order"] = ["A", "C"]
    assert run(loose=True) is None                                # different comps: never reused


def test_update_pending_flag_is_seller_only():
    seller = SimpleNamespace(mode="listing", subject={"seller_answers": {"update_pending": True}})
    buyer = SimpleNamespace(mode="buyer", subject={"seller_answers": {"update_pending": True}})
    assert ai.seller_update_pending(seller) and not ai.seller_update_pending(buyer)
    assert not ai.seller_update_pending(SimpleNamespace(mode="listing", subject={}))
