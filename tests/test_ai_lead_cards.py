"""Lead cards in the AI chat (11/18): which leads a reply names, from what the lookups returned."""
from types import SimpleNamespace

from app.services.ai import ai_lead_cards as lc

A = "11111111-2222-3333-4444-555555555555"
B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
C = "99999999-8888-7777-6666-555555555555"


def test_collect_reads_every_lookup_shape():
    seen: dict = {}
    lc.collect({"results": [{"id": A, "name": "Jane Smith", "represented": "no"}]}, seen)            # search_leads
    lc.collect({"follow_ups_due": [{"title": "Text Bo", "lead_id": B, "lead_name": "Bo Jones"}]}, seen)  # get_things_to_do
    lc.collect({"priorities": [{"id": f"lead-{C}-new_match", "title": "x", "lead_name": "Cy Young"}]}, seen)
    lc.collect({"items": [{"lead_id": "not-an-id", "name": "Zed Zed"}, {"lead_id": A, "name": "Solo"}]}, seen)
    assert seen == {A: "jane smith", B: "bo jones", C: "cy young"}


def test_test_suffix_and_card_facts_left_out():
    seen: dict = {}
    lc.collect({"lead_id": A, "name": "Jane Hotlead (Test)"}, seen)
    assert lc.mentioned("Start with Jane Hotlead.", seen) == [A]
    assert lc._SAME_AS_CARD.match("No agent") and lc._SAME_AS_CARD.match("new.")
    assert not lc._SAME_AS_CARD.match("No agent yet - asked about schools")


def test_mentioned_full_names_in_order():
    seen = {A: "jane smith", B: "bo jones", C: "cy young"}
    assert lc.mentioned("Start with **Bo Jones**, then Jane Smith.", seen) == [B, A]
    assert lc.mentioned("Jane might answer; Smithers won't.", seen) == []


def test_status_and_points():
    lead = SimpleNamespace(hidden_at=None, status="qualified")
    assert lc._status(lead) == ("in_talks", "In talks")
    assert lc._status(SimpleNamespace(hidden_at="x", status="new")) == ("hidden", "Hidden")
    assert lc._short("- " + "word " * 40, 30).endswith("…")
    assert len(lc._short("word " * 40, 30)) <= 31


def test_restated_card_facts_spotted():
    assert lc.restates_card("I found one lead.\n\nStatus: New, never contacted\nSource: Open house sign-in\nAgent status: Unknown")
    assert lc.restates_card("- **Status:** New\n- **Came in:** form")
    assert not lc.restates_card("Start with Jane Smith - most active, no agent.")
