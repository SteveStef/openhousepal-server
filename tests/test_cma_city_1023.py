"""10/23 Match % v4 + city fixes, from 1703 Point Breeze Ave (Philadelphia) and a 120-home Philadelphia
backtest: multi-family 0 baths, missing facts no longer a free pass, city market area, condition tiers,
neighborhood price level, city comp-search distances. Pure-function checks - no database."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.main  # noqa: F401,E402
from app.schemas.cma import CmaCompCandidate  # noqa: E402
from app.services import cma_ai_service as ai  # noqa: E402
from app.services import cma_service as svc  # noqa: E402

TODAY = date(2026, 10, 23)
CITY = {"distance_half_life_miles": 0.65, "band": "dense_urban"}

POINT_BREEZE = {"listing_key": "pb", "street_address": "1703 POINT BREEZE AVE", "city": "Philadelphia", "state": "PA",
                "zipcode": "19145", "property_type": "TOWNHOUSE", "year_built": 2023, "sqft": 2772,
                "above_grade_finished_area": 2772, "lot_size": 1030, "school_district": "The School District of Philadelphia",
                "township": "PHILADELPHIA", "subdivision_name": "POINT BREEZE", "latitude": 39.93087, "longitude": -75.18268,
                "property_condition": ["Excellent"]}


def _sale(key, **kw):
    base = {"listing_key": key, "status": "sold", "price_verified": True, "is_included": True, "property_type": "TOWNHOUSE",
            "city": "Philadelphia", "township": "PHILADELPHIA", "school_district": "The School District of Philadelphia",
            "close_date": "2026-09-01", "sale_price": 500000, "close_price": 500000, "sqft": 2500,
            "above_grade_finished_area": 2500, "lot_size": 1000, "year_built": 2020, "distance_miles": 0.3,
            "zipcode": "19145", "subdivision_name": "POINT BREEZE"}
    base.update(kw)
    return base


def _score(subject, comps, density=CITY):
    rel = ai._continuous_relevance(subject, comps, density_context=dict(density))
    return {r["listing_key"]: r for r in rel["comps"]}


# ---- missing facts -------------------------------------------------------------------------------------

def test_a_comp_missing_its_year_built_no_longer_outscores_one_with_a_close_year():
    rows = _score(POINT_BREEZE, [_sale("known", year_built=2021), _sale("unknown", year_built=None)])
    assert rows["unknown"]["components"]["age"] == ai._REL_UNKNOWN_CREDIT
    assert rows["known"]["base_relevance_score"] > rows["unknown"]["base_relevance_score"]


def test_a_sale_with_no_sale_date_scores_unknown_recency():
    rows = _score(POINT_BREEZE, [_sale("nodate", close_date=None, sale_date=None)])
    assert rows["nodate"]["components"]["recency"] == ai._REL_UNKNOWN_CREDIT


# ---- city market area -----------------------------------------------------------------------------------

def test_city_market_area_uses_neighborhood_not_the_shared_city_and_district():
    rows = _score(POINT_BREEZE, [_sale("pb2"), _sale("grays", subdivision_name="GRAYS FERRY", zipcode="19146")])
    assert rows["pb2"]["components"]["market_area"] == 1.0
    assert rows["grays"]["components"]["market_area"] == 0.0


def test_city_comp_across_the_city_line_scores_zero_market_area():
    rows = _score(POINT_BREEZE, [_sale("ud", city="Upper Darby", township="UPPER DARBY", school_district="Upper Darby")])
    assert rows["ud"]["components"]["market_area"] == 0.0


def test_city_distance_penalty_starts_at_a_quarter_mile():
    rows = _score(POINT_BREEZE, [_sale("near", distance_miles=0.2), _sale("mile", distance_miles=0.9)])
    assert rows["near"]["components"]["proximity"] == 1.0
    assert rows["mile"]["components"]["proximity"] < 0.6


# ---- condition ------------------------------------------------------------------------------------------

def test_condition_tier_reads_the_bright_field_first_then_the_description():
    assert ai._condition_tier({"property_condition": ["Major Rehab Needed"], "public_remarks": "fully renovated"}) == 0.0
    assert ai._condition_tier({"public_remarks": "Investor Opportunity! Ready for a full gut renovation."}) == 0.5
    assert ai._condition_tier({"public_remarks": "ONLINE AUCTION - South Philadelphia"}) == 0.5
    assert ai._condition_tier({"public_remarks": "Needs TLC"}) == 1.5
    assert ai._condition_tier({"public_remarks": "No work needed - fully renovated, new kitchen"}) == 4.0
    assert ai._condition_tier({"public_remarks": "Great two-story brick row home"}) is None


def test_a_gut_rehab_comp_scores_low_on_condition_for_a_renovated_home():
    rows = _score(POINT_BREEZE, [_sale("reno", public_remarks="Fully renovated"),
                                 _sale("shell", public_remarks="Investor special, sold as-is")])
    assert rows["reno"]["components"]["condition"] == 1.0
    assert rows["shell"]["components"]["condition"] < 0.1


def test_grid_moves_a_renovated_comp_down_for_a_rehab_subject():
    subject = {**POINT_BREEZE, "property_condition": ["Major Rehab Needed"]}
    comp = _sale("reno", property_condition=["Excellent"], _area_price={"urban": True})
    r = {"_c": comp, "price": 300000, "sqft": 2500, "ag": 2500, "bg": 0, "bg_estimated": False}
    feat = ai._v10_grid_adjustments(subject, r, 2500, 0, False, 120.0)
    # -37.5% from the condition index, held by the grid's +-30% net cap
    assert feat["condition"] < -80000
    assert abs(feat["total"]) <= round(ai._V10_FEATURE_MAX_PCT * 300000) + 1
    assert any("comp renovated / new vs subject needs major rehab" in n for n in feat["notes"])


# ---- neighborhood price level ---------------------------------------------------------------------------

def test_area_price_level_compares_the_comps_blocks_to_the_subjects():
    lat, lng = POINT_BREEZE["latitude"], POINT_BREEZE["longitude"]
    near_subject = [(f"s{i}", lat + 0.0005 * i, lng, 240.0) for i in range(8)]
    far = (lat + 0.02, lng - 0.02)   # ~1.6 mi away
    near_far = [(f"f{i}", far[0] + 0.0005 * i, far[1], 170.0) for i in range(8)]
    density = {**CITY, "_price_surface": near_subject + near_far}
    rows = _score(POINT_BREEZE, [_sale("home", latitude=lat + 0.001, longitude=lng),
                                 _sale("cheap", latitude=far[0], longitude=far[1], distance_miles=1.6)], density)
    assert rows["home"]["components"]["area_price"] == 1.0
    assert rows["cheap"]["components"]["area_price"] < 0.2
    assert rows["cheap"]["component_detail"]["area_price"]["ratio"] < 0.75


# ---- comp search ----------------------------------------------------------------------------------------

def _cand(i, d, days, score, band):
    return CmaCompCandidate(listing_key=f"c{i}", status="sold", street_address=f"{i + 10} ELM ST", distance_miles=d,
                            sale_date=(TODAY - timedelta(days=days)).isoformat(), match_score=score,
                            similarity_details={"valuation_eligible": True, "density_context": {"band": band}})


def test_city_rowhome_search_uses_city_distances_even_with_few_priced_sales():
    pool = [_cand(i, 0.1 + 0.05 * i, 30, 80, "dense_urban") for i in range(8)]
    assert svc.plan_auto_search(pool, "attached", today=TODAY)["density"] == "urban"
    assert svc.plan_auto_search(pool, "multi", today=TODAY)["density"] == "urban"
    sub = [_cand(i, 0.1 + 0.05 * i, 30, 80, "suburban") for i in range(8)]
    assert svc.plan_auto_search(sub, "attached", today=TODAY)["density"] == "suburban"
