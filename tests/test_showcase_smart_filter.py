"""Showcase smart filter rules (services/showcase_smart_filter.py) - no AI, no database."""
from app.services import showcase_smart_filter as sf


def test_wants_are_trimmed_capped_and_unique():
    assert sf.clean_wants(["  First-floor primary  ", "first-floor PRIMARY", "Pool", "9 ft+ ceilings", "Garage"]) == ["First-floor primary", "Pool", "9 ft+ ceilings"]
    assert sf.clean_wants("pool, fenced yard\n home office; extra") == ["pool", "fenced yard", "home office"]
    assert sf.clean_wants({"wants": ["x" * 90]}) == ["x" * 60]
    assert sf.clean_wants(None) == [] and sf.clean_wants([" ", ""]) == []


def test_key_ignores_order_and_case():
    assert sf.wants_key(["Pool", "9 ft+ ceilings"]) == sf.wants_key(["9 FT+ ceilings", "pool"])


def test_a_home_needs_every_want():
    yes, maybe = {"v": "yes"}, {"v": "maybe"}
    assert sf._all_yes([yes, yes]) == "yes"
    assert sf._all_yes([yes, maybe]) == "maybe"
    assert sf._all_yes([yes, None]) == "no"


def test_card_line_only_for_yes():
    assert sf.match_line(None) is None
    assert sf.match_line({"v": "maybe", "found": []}) is None
    line = sf.match_line({"v": "yes", "found": [{"w": "Pool", "q": "an in-ground ⟦pool⟧"}, {"w": "9 ft+ ceilings", "q": ""}]})
    # marks = the listing's own words to highlight in the description (only where the quote marked some)
    assert line == {"wants": ["Pool", "9 ft+ ceilings"], "quotes": ["an in-ground ⟦pool⟧"],
                    "marks": [{"want": "Pool", "words": "pool"}]}
