"""Follow-up message editor (owner, 11/21): the agent sees the open-house text as it reads when the guest's Showcase has
must-haves, can place a "What they must have" pill, and a must-have change reaches the text at once."""

from app.services import tasks_service as ts


def test_must_line_pill_is_offered():
    assert "must_line" in ts.PITCH_TOKENS
    assert ts.PITCH_TOKENS["must_line"]["sample"].endswith(".")


def test_preview_with_must_haves_reads_like_the_real_text():
    plain = ts.render_pitch(ts.DEFAULT_PITCHES["open_house_signin"], {**{k: t["sample"] for k, t in ts.PITCH_TOKENS.items()}, "must_line": ""})
    parts = {k: ts.PITCH_TOKENS[k]["sample"] for k in ("home_count", "best_home", "why_now", "showcase_link")}
    parts.update(ts._must_parts(14, None, ts.SAMPLE_MUST))
    tpl = ts._with_must(ts._count_words(ts.SMART_PITCHES["open_house_signin"], parts), parts)
    smart = ts.render_pitch(tpl, parts)
    assert "2 of them have a pool and a fenced yard." in smart
    assert "might interest you" in smart and "similar to the one you toured" not in smart
    assert "2 of them" not in plain


def test_agents_own_pitch_keeps_their_words_and_gains_the_sentence():
    parts = {k: ts.PITCH_TOKENS[k]["sample"] for k in ("home_count", "best_home", "why_now", "showcase_link")}
    parts.update(ts._must_parts(14, None, ts.SAMPLE_MUST))
    mine = "I made you a home search with {home_count} for you. Here: {showcase_link}"
    out = ts.render_pitch(ts._with_must(ts._count_words(mine, parts), parts), parts)
    assert out.startswith("I made you a home search with 14 homes for you. 2 of them have a pool and a fenced yard.")
    # the pill placed by hand goes exactly there; with no must-haves it leaves nothing behind
    placed = "{must_line} Your search: {showcase_link}"
    assert ts.render_pitch(ts._with_must(placed, parts), parts).startswith("2 of them have a pool and a fenced yard. Your search:")
    empty = {k: ts.PITCH_TOKENS[k]["sample"] for k in ("home_count", "showcase_link")}
    assert ts.render_pitch(ts._with_must(placed, empty), empty).startswith("Your search:")


def test_must_have_change_rebuilds_the_text_at_once():
    ts._store_pitch("tok-1121", (14, None, None, None, None, None))
    assert ts._cached_pitch("tok-1121") is not None
    ts.forget_pitch("tok-1121")
    assert ts._cached_pitch("tok-1121") is None
    ts.forget_pitch(None)   # never fails
