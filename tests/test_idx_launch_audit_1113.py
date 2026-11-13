"""IDX website launch audit 11/13 - home cards never read "0.00 ac" (condos and units carry a 0 lot)."""
from types import SimpleNamespace

import pytest

from app.api import public_site_routes  # noqa: F401 - loads the app modules in their usual order
from app.services.website import content_adapters
from app.services.website.content_adapters import card_acres


@pytest.mark.parametrize("raw, shown", [
    (None, None),
    (0, None),
    (0.0, None),
    (0.004, None),
    ("0", None),
    ("not a number", None),
    (0.005, 0.005),
    (0.04, 0.04),
    (1.2, 1.2),
    ("2.5", 2.5),
])
def test_card_acres_hides_zero_lots(raw, shown):
    assert card_acres(raw) == shown


def _home(**kw):
    base = dict(
        listing_key="K1", street_address="1030 LANCASTER AVE #418", full_address=None, city="BRYN MAWR", state="PA",
        zipcode="19010", price=209900, bedrooms=2, bathrooms=1, living_area=830, lot_size_acres=0.0,
        home_status="Active", img_src=None, latitude=40.0, longitude=-75.3, list_office_name="BHHS",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_search_card_drops_zero_lot():
    card = content_adapters._search_result_card(_home())
    assert card["lot_size_acres"] is None


def test_search_card_keeps_real_lot():
    card = content_adapters._search_result_card(_home(lot_size_acres=0.54))
    assert card["lot_size_acres"] == 0.54


def test_search_card_repairs_number_only_street():
    """Rows saved before the 11/13 address fix hold just "170" for "170, 200, 220 OLD STATE RD"."""
    card = content_adapters._search_result_card(_home(street_address="170", unparsed_address="170, 200, 220 OLD STATE RD"))
    assert card["address"].startswith("170") and "Old State" in card["address"]


def test_listing_card_without_unparsed_address_keeps_street():
    card = content_adapters._search_result_card(_home(street_address="12 Elm St"))
    assert card["address"] == "12 Elm St"
