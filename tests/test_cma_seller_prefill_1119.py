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
