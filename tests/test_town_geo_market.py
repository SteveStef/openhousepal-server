"""Town pages: geography and MLS-number rules from the 10/27 outside fact-check of 19 towns -
one shared 12-month window, every county (and Virginia's independent cities), a town's own ZIPs
only, no commute "to" the town itself, honest walkability, and the thin-MLS-coverage guard."""
from datetime import date

import pytest

from app.models.database import Property
from app.services.areas import market_stats_service as mss
from app.services.areas import town_extras_service as tes
from app.services.areas import town_map_service as tms
from app.services.areas.area_enrichment_service import zip_belongs_to_town


# ---------------------------------------------------------------- 1. one 12-month window
@pytest.mark.parametrize("today, start", [
    (date(2026, 10, 27), date(2025, 11, 1)),   # Ardmore's 10/27 page: chart, gauge and DOM all from Nov 1
    (date(2026, 1, 15), date(2025, 2, 1)),
    (date(2026, 12, 31), date(2026, 1, 1)),
    (date(2024, 2, 29), date(2023, 3, 1)),
])
def test_sales_window_starts_on_the_first_of_the_month_eleven_months_back(today, start):
    assert mss.sales_window(today) == (start, today)


def test_closed_sales_filter_bounds_both_ends_and_price():
    sql = str(mss.closed_sales_filter(Property, date(2025, 11, 1), date(2026, 10, 27)).compile(compile_kwargs={"literal_binds": True}))
    assert "2025-11-01" in sql and "2026-10-27" in sql      # no future close dates
    assert "50000" in sql                                    # $1 transfers aren't sales
    assert "CLOSED-BRIGHT" in sql


def test_gauge_shares_the_window():
    from app.services.areas import market_insights_service as mis
    assert mis.sales_window is mss.sales_window
    assert mis.PACE_MONTHS == 6


# ---------------------------------------------------------------- 2. counties
def _feat(name, base, lsadc="06", cc="H1"):
    return {"attributes": {"NAME": name, "BASENAME": base, "LSADC": lsadc, "COUNTYCC": cc}}


def test_every_county_with_a_real_share_is_kept():
    rows = tms.county_rows([_feat("Delaware County", "Delaware"), _feat("Chester County", "Chester"),
                            _feat("Montgomery County", "Montgomery"), _feat("Ocean County", "Ocean")], [45, 39, 16, 0])
    assert [r["name"] for r in rows] == ["Delaware County", "Chester County", "Montgomery County"]
    assert tms.county_label(rows) == "Delaware, Chester and Montgomery counties"   # Wayne, PA


def test_two_counties_read_naturally():
    rows = tms.county_rows([_feat("Montgomery County", "Montgomery"), _feat("Delaware County", "Delaware")], [54, 46])
    assert tms.county_label(rows) == "Montgomery and Delaware counties"   # Ardmore, PA


def test_a_two_percent_sliver_is_not_a_county_of_the_town():
    rows = tms.county_rows([_feat("Delaware County", "Delaware"), _feat("Chester County", "Chester")], [98, 2])
    assert tms.county_label(rows) == "Delaware County"


def test_virginia_independent_city_is_not_a_county():
    city = _feat("Charlottesville city", "Charlottesville", lsadc="25", cc="C7")
    assert tms._county_kind(city["attributes"]) == "independent_city"
    assert tms.county_label(tms.county_rows([city], [100])) == "Independent city"
    mixed = tms.county_rows([_feat("Albemarle County", "Albemarle"), city], [71, 26])
    assert tms.county_label(mixed) == "Charlottesville (independent city) and Albemarle County"


def test_apply_counties_never_stores_an_independent_city_as_the_county():
    from types import SimpleNamespace
    from unittest.mock import patch
    area = SimpleNamespace(facts={}, county="Albemarle County")
    with patch.object(tms, "flag_modified"):
        tms._apply_counties(area, [{"name": "Charlottesville", "share": 80, "kind": "independent_city"}])
    assert area.county is None and area.facts["county_label"] == "Independent city"
    area = SimpleNamespace(facts={}, county=None)
    with patch.object(tms, "flag_modified"):
        tms._apply_counties(area, [{"name": "Delaware County", "share": 45, "kind": "county"},
                                   {"name": "Chester County", "share": 39, "kind": "county"}])
    assert area.county == "Delaware County"   # Wayne's county was blank


# ---------------------------------------------------------------- 3. ZIPs
@pytest.mark.parametrize("zip_listings, named, township, belongs", [
    (200, 3, 0, False),      # 22923: 197 of 200 listings say Barboursville, not Charlottesville
    (340, 2, 0, False),      # 08550: Princeton Junction / West Windsor
    (1031, 1012, 0, True),   # 08540: Princeton
    (1835, 230, 1100, True), # 19087 for Radnor Township: most homes sit in the township
    (0, 0, 0, False),
])
def test_a_zip_joins_only_when_it_is_the_towns_postal_area(zip_listings, named, township, belongs):
    assert zip_belongs_to_town(zip_listings, named, township) is belongs


# ---------------------------------------------------------------- 4. commute
def test_commute_never_goes_to_the_town_itself():
    commute = {"destinations": [
        {"name": "Center City Philadelphia", "kind": "city", "drive_minutes": 70, "miles": 45},
        {"name": "Princeton", "kind": "job_center", "drive_minutes": 4, "miles": 1.2},
        {"name": "Newark Liberty (EWR)", "kind": "airport", "drive_minutes": 50, "miles": 40},
    ]}
    out = tes.commute_for_page(commute, "Princeton", 40.3487, -74.6593)
    assert [d["name"] for d in out["destinations"]] == ["Center City Philadelphia", "Newark Liberty (EWR)"]
    cv = {"destinations": [{"name": "Downtown Charlottesville", "kind": "job_center", "drive_minutes": 9, "miles": 3.5},
                           {"name": "Charlottesville (CHO)", "kind": "airport", "drive_minutes": 15, "miles": 9}]}
    assert [d["name"] for d in tes.commute_for_page(cv, "Charlottesville", 38.0293, -78.4767)["destinations"]] == ["Charlottesville (CHO)"]


def test_a_far_city_sharing_the_towns_name_still_counts():
    assert not tes.is_self_destination("Downtown Washington, D.C.", "city", "Washington", 40.174, -80.246)


def test_pick_destinations_skips_the_town():
    names = [d["name"] for d in tes.pick_destinations(40.3487, -74.6593, "Princeton")]
    assert "Princeton" not in names and names[0] == "Center City Philadelphia"


# ---------------------------------------------------------------- 5. walkability
def test_tourist_depot_is_not_a_train_station():
    gettysburg = {"name": "Gettysburg & Harrisburg Railroad Depot", "railway": "station", "public_transport": "station", "train": "yes",
                  "description": "Former Reading Railroad Station built in 1884. Used by new tourist rail operation owned by Patriot Rail."}
    assert tms.station_kind(gettysburg) is None
    assert tms.station_kind({"railway": "station", "name": "Strasburg", "usage": "tourism"}) is None
    assert tms.station_kind({"railway": "station", "name": "Old Depot", "railway:historic": "station"}) is None


def test_working_station_in_a_historic_building_still_counts():
    assert tms.station_kind({"railway": "station", "name": "Bryn Mawr", "network": "SEPTA", "historic": "railway_station"}) == "train"
    assert tms.station_kind({"railway": "station", "name": "Ardmore", "network": "Amtrak;SEPTA", "train": "yes"}) == "train"


def test_metro_m_stops_are_trolley_stops():
    assert tms.station_kind({"railway": "station", "name": "Ardmore Junction", "network": "SEPTA", "station": "light_rail", "light_rail": "yes"}) == "trolley"
    assert tms.station_kind({"railway": "tram_stop", "name": "Ardmore Avenue"}) is None   # not part of the walk query


@pytest.mark.parametrize("pct, words", [(100, "Nearly all"), (95, "Nearly all"), (94, "94%"), (0, "0%"), (None, None)])
def test_walk_percent_words(pct, words):
    assert tms.walk_pct_words(pct) == words


# ---------------------------------------------------------------- 6. thin MLS coverage
def test_charlottesville_local_copy_is_thin():
    v = mss.coverage_verdict(active=12, sales_12=8, under_contract=202, stale_under_contract=200, households=19699)
    assert v["thin"] and set(v["reasons"]) == {"stale_under_contract", "low_mls_volume"}


def test_charlottesville_live_is_thin_on_volume_alone():
    v = mss.coverage_verdict(active=13, sales_12=32, under_contract=2, stale_under_contract=0, households=19699)
    assert v["thin"] and v["reasons"] == ["low_mls_volume"]


@pytest.mark.parametrize("active, sales, uc, stale, hh", [
    (18, 118, 24, 1, 6487),      # Ardmore
    (108, 89, 51, 4, 11437),     # State College (lowest normal town, 17 per 1,000)
    (9, 62, 7, 0, 2353),         # Haverford
    (247, 94, 59, 2, 804),       # Rehoboth Beach - too few households to judge by volume
    (214, 129, 56, 3, None),     # Rittenhouse Square - no household count
    (40, 30, 30, 12, 5000),      # a few stale contracts don't trip it
])
def test_normal_towns_are_not_thin(active, sales, uc, stale, hh):
    assert not mss.coverage_verdict(active, sales, uc, stale, hh)["thin"]


def test_guard_drops_only_the_sales_based_numbers():
    payload = {
        "facts": {"market_stats": {"active_count": 12, "median_price": 500000, "under_contract_count": 202,
                                   "median_sold_dom": 30, "min_price": 1, "max_price": 2, "price_range_basis": "sold_2y"}},
        "market_trends": {"series": [1], "closed_count": 8},
        "insights": {"gauge": {"label": "Buyer's market"}, "money_buys": [1, 2], "taxes": {"per_sqft": 2.1}},
        "market_sales": [{"x": 1}],
    }
    out = mss.apply_coverage_guard(payload, {"thin": True, "reasons": ["low_mls_volume"]})
    assert out["facts"]["market_stats"] == {"active_count": 12, "median_price": 500000}
    assert out["market_trends"] == {}
    assert out["insights"] == {"taxes": {"per_sqft": 2.1}}
    assert out["market_sales"] == [{"x": 1}]
    assert out["market_coverage"]["thin"] is True


def test_guard_leaves_a_normal_town_alone():
    payload = {"market_trends": {"series": [1]}}
    assert mss.apply_coverage_guard(payload, {"thin": False}) == {"market_trends": {"series": [1]}}
