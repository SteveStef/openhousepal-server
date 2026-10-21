"""Leads search by the homes a lead came through - open house / Cheat Sheet, never Showcase homes (10/21)."""
import sys
from pathlib import Path
from types import SimpleNamespace

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.api.lead_routes import _lead_places  # noqa: E402


def ev(st, addr=None, oh=None):
    return SimpleNamespace(source_type=st, property_address=addr, open_house_event=oh)


def test_places_include_open_house_and_cheat_sheet_but_not_showcase_homes():
    oh = SimpleNamespace(address="417 Dogwood Ln", city="Coatesville")
    places = _lead_places([
        ev("open_house_visitor", "417 Dogwood Ln", oh),
        ev("property_website_inquiry", "12 Oak St"),
        ev("showcase_like", "99 Showcase Rd"),
        ev("property_comment", "88 Comment Ave"),
    ])
    assert "417 Dogwood Ln" in places and "417 Dogwood Ln, Coatesville" in places and "12 Oak St" in places
    assert not any("Showcase" in p or "Comment" in p for p in places)
