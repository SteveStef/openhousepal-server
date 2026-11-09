"""The helper's one-call Showcase search (owner, 11/08: "check the description of every home in
Nicole's Showcase for a first-floor bedroom" ran past the chat's time limit opening homes one by one)."""
import asyncio
from types import SimpleNamespace

from app.services.ai import ai_tools


def _home(key, desc="", feats=None, raw=None, levels=None):
    return SimpleNamespace(id="id-" + key, listing_key=key, full_address=f"{key} Main St", street_address=f"{key} Main St", city="Wayne", state="PA",
                           price=500000, bedrooms=4, bathrooms=2, living_area=2000, home_status="ACTIVE-BRIGHT",
                           description=desc, interior_features=feats or [], raw_mls_data=raw or {}, levels=levels or [],
                           listing_contract_date=None, on_market_date=None, days_on_market=None, cumulative_days_on_market=None)


HOMES = [
    _home("A", "Lovely colonial. The first-floor primary bedroom has a walk-in closet. Big yard."),
    _home("B", "Three bedrooms upstairs. Finished basement.", raw={"Levels": ["2"], "BedroomsTotal": 3, "UpperLevelsTotalBedrooms": 3}),
    _home("C", "Charming home.", feats=["Entry Level Bedroom", "Wood Floors"]),
    _home("D", "Updated kitchen.", raw={"Levels": ["2"], "BedroomsTotal": 4, "UpperLevelsTotalBedrooms": 3, "LowerLevelsTotalBedrooms": 0}),
    _home("E", "Ranch living at its best.", raw={"Levels": ["1"], "BedroomsTotal": 3}),
    _home("F", ""),
    _home("G", "Bedroom on the main level, perfect for guests."),
]


def _run(look_for, monkeypatch):
    async def fake(db, user, lead_id):
        return SimpleNamespace(id=lead_id), SimpleNamespace(id="col1", properties=HOMES)
    monkeypatch.setattr(ai_tools, "_find_lead_collection", fake)
    monkeypatch.setattr(ai_tools, "property_days_on_market", lambda p: None)

    async def words_only(db, user, config, question, homes, words):   # the free word pass alone
        return words
    monkeypatch.setattr(ai_tools, "_ai_read", words_only)
    return asyncio.run(ai_tools._search_showcase_homes(None, SimpleNamespace(id="u"), {"lead_id": "L1", "look_for": look_for}, {}))


def test_first_floor_bedroom_finds_description_feature_and_room_counts(monkeypatch):
    out = _run(["first floor bedroom", "main level bedroom", "entry level bedroom"], monkeypatch)
    found = {m["listing_key"]: m["found"] for m in out["matches"]}
    assert out["homes_checked"] == 7
    assert "first-floor primary bedroom" in found["A"][0]                  # words in between still match
    assert found["C"] == ["MLS feature list: Entry Level Bedroom"]
    assert found["D"] == ["MLS room counts: 1 of 4 bedrooms on the main floor"]
    assert "main level" in found["G"][0]
    assert "B" not in found and "F" not in found                           # all bedrooms upstairs / nothing to read
    assert [h["listing_key"] for h in out["one_level_homes"]] == ["E"]     # a ranch is listed apart, not as a match
    assert out["homes_with_no_description"] == 1


def test_other_questions_skip_the_floor_facts(monkeypatch):
    out = _run(["finished basement"], monkeypatch)
    assert [m["listing_key"] for m in out["matches"]] == ["B"]
    assert "one_level_homes" not in out


def test_needs_something_to_look_for(monkeypatch):
    assert _run([], monkeypatch)["error"] == "missing"


def test_tool_is_registered():
    assert "search_showcase_homes" in ai_tools._DISPATCH
    assert any(t["name"] == "search_showcase_homes" for t in ai_tools.TOOL_DEFINITIONS)


def test_words_far_apart_or_in_other_sentences_dont_count():
    words = ai_tools._phrase_words("first floor bedroom")
    assert ai_tools._phrase_span("The first floor has a den. Three bedrooms upstairs.", words) is None
    assert ai_tools._phrase_span("The first floor features a large family room with a fireplace while upstairs are four bedrooms", words) is None
    assert ai_tools._phrase_span("A 1st floor bedroom", ai_tools._phrase_words("1st floor bedroom"))


def test_an_unanswered_question_isnt_replayed():
    """A failed turn's question stayed in the chat and every later message tried that job again."""
    from app.api import ai_routes
    msgs = [SimpleNamespace(role=r, content=c, tool_calls=None) for r, c in
            [("user", "hi"), ("assistant", "hello"), ("user", "check all 500 homes"), ("user", "what's 2+2"), ("assistant", "4"), ("user", "dangling")]]

    class DB:
        async def execute(self, stmt):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(reversed(msgs))))
    conv = SimpleNamespace(id="c", summary_covers_through=None, summary=None)
    history, _ = asyncio.run(ai_routes._build_history(DB(), conv, 50))
    assert [h["content"] for h in history] == ["hi", "hello", "what's 2+2", "4"]


def test_town_search_is_mls_only():
    assert "search_homes_for_sale" in ai_tools._MLS_TOOLS


def test_remove_card_only_removes_homes_on_the_card():
    from app.services.ai import ai_actions
    from app.services.ai.ai_actions import ActionError
    ai_actions._load()
    adj = ai_actions.ACTIONS["remove_showcase_homes"].adjust
    payload = {"collection_id": "c", "homes": [{"id": "a"}, {"id": "b"}, {"id": "c"}], "selected": ["a", "b"]}
    assert adj(None, payload, {"property_ids": ["b", "zzz"]})["selected"] == ["b"]   # a home not on the card is ignored
    try:
        adj(None, payload, {"property_ids": []})
        assert False, "nothing checked must not run"
    except ActionError:
        pass
    assert "remove_showcase_homes" in {d["name"] for d in ai_actions.action_tools_for(SimpleNamespace(is_admin=False, plan_tier="PREMIUM"))}
    assert "remove_showcase_homes" not in {d["name"] for d in ai_actions.action_tools_for(SimpleNamespace(is_admin=False, plan_tier="BASIC"))}


def test_next_message_is_told_the_ids_the_last_reply_used():
    from app.api import ai_routes
    refs = [{"record_type": "lead", "record_id": "L-123"}, {"record_type": "showcase", "record_id": "C-9"}, {"record_type": "lead", "record_id": "L-123"}]
    msgs = [SimpleNamespace(role="user", content="which homes have a first floor bedroom", tool_calls=None),
            SimpleNamespace(role="assistant", content="Three do.", tool_calls=refs),
            SimpleNamespace(role="user", content="remove the others", tool_calls=None),
            SimpleNamespace(role="assistant", content="Here's the card.", tool_calls=None)]

    class DB:
        async def execute(self, stmt):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(reversed(msgs))))
    history, _ = asyncio.run(ai_routes._build_history(DB(), SimpleNamespace(id="c", summary_covers_through=None, summary=None), 50))
    nxt = history[2]["content"]
    assert "lead_id L-123" in nxt and "collection_id C-9" in nxt and nxt.count("L-123") == 1
    assert nxt.endswith("remove the others")


def test_a_reply_listing_homes_is_caught():
    from app.services.ai import ai_showcase_cards as sc
    assert sc.lists_homes("Not real: 606 Longchamps Dr (pool table), 37 Sagewood Dr #14, 514 Old Forge Xing")
    assert not sc.lists_homes("2 have a pool: 6 Hunt Club Ln and 404 Chester Rd. 9 others aren't real.")


# --- AI reader (owner, 11/08: "if it's not AI it won't catch the right stuff") ---------------------
from app.services.ai import ai_home_reader, ai_home_search

CFG = {"model": "claude-haiku-5-5", "model_pricing": {"claude-haiku-5-5": {"input_per_mtok": 0.10, "output_per_mtok": 0.50}}}


def _fake_client(answer):
    async def create(**kw):
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=5000, output_tokens=200), content=[SimpleNamespace(type="text", text=answer)])
    return SimpleNamespace(messages=SimpleNamespace(create=create))


def test_reader_keeps_real_quotes_and_drops_made_up_ones(monkeypatch):
    homes = [_home("A", "Lovely colonial. The first-floor primary bedroom has a walk-in closet."), _home("B", "Owner's retreat on the main level."),
             _home("C", "Three bedrooms upstairs.")]
    answer = ('Sure: {"hits":[{"n":1,"q":"The first-floor primary bedroom has a walk-in closet","why":"says first floor","v":"yes"},'
              '{"n":2,"q":"primary suite on the first floor","why":"owner retreat main level","v":"maybe"},'
              '{"n":3,"q":"Three bedrooms upstairs","why":"all upstairs","v":"no"}, {"n":9,"v":"yes"}]}')
    monkeypatch.setattr(ai_home_reader, "_get_client", lambda: _fake_client(answer))
    out = asyncio.run(ai_home_reader.read(CFG, "a first-floor primary bedroom", homes))
    v = out["verdicts"]
    assert v["id-A"]["v"] == "yes" and v["id-A"]["quote"].startswith(ai_home_reader.HL_OPEN + "The first-floor primary")
    assert v["id-B"]["v"] == "maybe" and v["id-B"]["quote"] == ""      # not really in the listing - never shown
    assert "id-C" not in v                                             # "no" is left out; #9 doesn't exist
    assert out["usd"] > 0 and out["read"] == ["id-A", "id-B", "id-C"] and not out["failed"]


def test_reader_survives_a_cut_off_answer(monkeypatch):
    homes = [_home("A", "Big in-ground pool."), _home("B", "Nice yard.")]
    answer = '{"hits":[{"n":1,"q":"Big in-ground pool","why":"pool","v":"yes"},{"n":2,"q":"Nice'
    monkeypatch.setattr(ai_home_reader, "_get_client", lambda: _fake_client(answer))
    out = asyncio.run(ai_home_reader.read(CFG, "a pool", homes))
    assert list(out["verdicts"]) == ["id-A"] and not out["failed"]


def test_estimate_grows_with_homes_and_is_a_percent_on_screen():
    one = ai_home_reader.estimate_usd(CFG, 1500 * 100, 100)
    three = ai_home_reader.estimate_usd(CFG, 1500 * 300, 300)
    assert 0 < one < three < 0.1
    assert ai_home_reader.percent_text(ai_home_reader.percent(0.0001, 2.5)) == "under 0.1%"
    assert ai_home_reader.percent(0.025, 2.5) == 1.0


def test_home_search_needs_place_beds_size_and_question():
    f = ai_home_search.clean({"town": "Exton, PA", "min_beds": "3", "min_sqft": "1,500", "question": "pool"})
    assert f["town"] == "Exton" and f["min_beds"] == 3 and f["min_sqft"] == 1500 and ai_home_search.missing(f) == []
    assert ai_home_search.missing(ai_home_search.clean({"question": "pool"})) == ["place", "min_beds", "min_sqft"]
    assert ai_home_search.missing(ai_home_search.clean({"district": "West Chester Area", "min_beds": 2, "min_sqft": 1000})) == ["question"]
