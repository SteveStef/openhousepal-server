"""The AI blog writer never publishes fill-in-the-blank text or a heading with nothing under it
(11/01: a live Main Line article showed "My current local numbers" with "$___" lines)."""
from app.services.ai import ai_blog_writer_service as w


def _article(sections, **extra):
    return w._normalize_article({"title": "T", "intro": extra.get("intro", ["Intro."]), "at_a_glance": extra.get("glance", []),
                                 "sections": sections, "closing": {"heading": "Talk to me", "paragraphs": ["Call me."], "button_label": "Go"}})


def test_placeholder_list_and_its_subheading_are_removed():
    a = _article([{"heading": "Prices now", "blocks": [
        {"type": "paragraph", "text": "Start with what can be counted."},
        {"type": "subheading", "text": "My current local numbers"},
        {"type": "bulleted_list", "items": ["[Median sale price for (area), 90 days ending (date): $___]", "[Median days on market: ___]"]},
        {"type": "paragraph", "text": "Rates matter too."},
    ]}])
    removed = w.strip_placeholders(a)
    kinds = [b["type"] for b in a["sections"][0]["blocks"]]
    assert kinds == ["paragraph", "paragraph"]
    assert len(removed) == 2


def test_real_lists_and_brackets_survive():
    a = _article([{"heading": "Towns", "blocks": [
        {"type": "bulleted_list", "items": ["Wayne: median sale price $926K (90 days ending Oct 19, 2026)", "Ardmore [see the card below]: $505K"]},
    ]}])
    assert w.strip_placeholders(a) == []
    assert len(a["sections"][0]["blocks"][0]["items"]) == 2


def test_section_left_empty_is_dropped_and_no_bare_heading_is_emitted():
    a = _article([
        {"heading": "Your numbers", "blocks": [{"type": "paragraph", "text": "[insert your local stats here]"}]},
        {"heading": "Empty", "blocks": []},
        {"heading": "Real", "blocks": [{"type": "paragraph", "text": "Homes sold in about a week."}]},
    ])
    w.strip_placeholders(a)
    blocks = w.article_to_blocks(a)
    headings = [b["data"]["html"] for b in blocks if b["type"] == "heading"]
    assert headings == ["Real", "Talk to me"]
    assert "insert" not in w.blocks_to_text(blocks)


def test_article_to_blocks_skips_heading_without_content():
    a = _article([{"heading": "Only a subheading", "blocks": [{"type": "subheading", "text": "Sub"}]},
                  {"heading": "Kept", "blocks": [{"type": "paragraph", "text": "Body."}]}])
    heads = [b["data"]["html"] for b in w.article_to_blocks(a) if b["type"] == "heading"]
    assert heads == ["Kept", "Talk to me"]


def test_intro_glance_and_closing_placeholders_removed():
    a = _article([{"heading": "A", "blocks": [{"type": "paragraph", "text": "Body."}]}],
                 intro=["Real intro.", "Median price: $___"], glance=["Rates were 6.2% (Freddie Mac).", "{{town}} inventory TBD"])
    a["closing"]["paragraphs"].append("[Add your phone number]")
    removed = w.strip_placeholders(a)
    assert a["intro"] == ["Real intro."] and a["at_a_glance"] == ["Rates were 6.2% (Freddie Mac)."]
    assert a["closing"]["paragraphs"] == ["Call me."] and len(removed) == 3


def test_a_line_that_is_only_a_bracketed_note_is_removed():
    # Live 11/01: "[What these figures cover: township or mailing address]" stayed on the page.
    a = _article([{"heading": "Prices now", "blocks": [
        {"type": "paragraph", "text": "Start with what can be counted."},
        {"type": "subheading", "text": "My current local numbers"},
        {"type": "paragraph", "text": "[What these figures cover: township or mailing address]"},
        {"type": "paragraph", "text": "Rates matter too [see below]."},
    ]}])
    removed = w.strip_placeholders(a)
    assert [b["type"] for b in a["sections"][0]["blocks"]] == ["paragraph", "paragraph"]
    assert removed == ["[What these figures cover: township or mailing address]"]
