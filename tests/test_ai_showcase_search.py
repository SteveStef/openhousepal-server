"""The helper's one-call Showcase search (owner, 11/08: "check the description of every home in
Nicole's Showcase for a first-floor bedroom" ran past the chat's time limit opening homes one by one)."""
import asyncio
from types import SimpleNamespace

from app.services.ai import ai_tools


def _home(key, desc="", feats=None, raw=None, levels=None):
    return SimpleNamespace(listing_key=key, full_address=f"{key} Main St", street_address=f"{key} Main St", city="Wayne", state="PA",
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
