"""Owner 11/19 (watching a real Fast Cut of 239 Windham Dr): "after the beds and baths title screen it shows kitchen
stuff". The shot after "Beds · Baths" is now another bedroom or bathroom when the reel has one - photos trade places,
every shot keeps its spot on the beat, and two of the same kind of room never end up back to back."""
import json
import pathlib

import app.main  # noqa: F401
from app.services import fast_cut_recipe

FIX = pathlib.Path(__file__).parent / "fixtures" / "fastcut_windham_1119.json"
BEDBATH = ("bedroom", "bathroom")


def _plan():
    motion = json.loads(FIX.read_text())
    return motion, fast_cut_recipe.plan(motion, motion.get("format") or "9:16")


def test_beds_baths_leads_into_the_bedrooms_then_the_bathrooms():
    _, props = _plan()
    shots = props["shots"]
    sb = next(i for i, s in enumerate(shots) if (s.get("fact") or {}).get("label", "").startswith("Beds"))
    assert shots[sb]["kind"] == "bedroom"                       # beds first: the card sits on a bedroom
    run = []
    for s in shots[sb + 1:-1]:
        if s["kind"] not in BEDBATH:
            break
        run.append(s["kind"])
    others = [s["kind"] for i, s in enumerate(shots[1:-1], 1) if i != sb and s["kind"] in BEDBATH]
    assert len(run) == len(others) >= 2                         # every other bedroom / bathroom follows the card
    assert run == sorted(run, key=lambda k: k != "bedroom")      # bedrooms, then bathrooms


def test_timing_kept_and_no_repeats_side_by_side():
    _, props = _plan()
    shots = props["shots"]
    for a, b in zip(shots, shots[1:]):
        assert abs(a["end"] - b["start"]) < 1e-6               # back to back on the beat, no gaps
        assert a["photo"] != b["photo"]                          # never the same photo twice in a row
    assert shots[0]["kind"] == "exterior" and shots[-1]["kind"] == "exterior"   # opens and closes on the front


def test_an_agents_own_order_is_left_alone():
    motion, props = _plan()
    order = [s["photo"] for s in props["shots"][1:-1]]
    custom = list(reversed(order))
    motion.setdefault("fast", {})["order"] = custom
    again = fast_cut_recipe.plan(motion, motion.get("format") or "9:16")
    got = [s["photo"] for s in again["shots"][1:-1]]
    # the rule may not touch what the agent set (only the beds/baths follow-up can differ when it runs on auto)
    assert got[:5] == [n for n in custom if n in got][:5]
