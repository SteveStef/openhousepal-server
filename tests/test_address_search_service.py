"""Unit tests for the CMA subject address search (app/services/address_search_service.py).
Pure functions only - no database."""
from datetime import datetime, timezone

import pytest

from app.services.address_search_service import (
    Candidate, RENTAL_NOTE, build_candidate_sql, group_and_pick, norm_unit, parse_query, pick_record, score_candidate,
)


def cand(street, *, key=None, city="PHILADELPHIA", state="PA", zipcode="19102", status="ACTIVE-BRIGHT",
         ptype="Residential", price=100000.0, modified=(2026, 1, 1)):
    return Candidate(
        listing_key=key or f"{street}|{status}|{modified}", street_address=street,
        full_address=f"{street}, {city}, {state} {zipcode}", city=city, state=state, zipcode=zipcode,
        home_status=status, mls_property_type=ptype, price=price,
        modified=datetime(*modified, tzinfo=timezone.utc),
    )


# ---- parsing ---------------------------------------------------------------------------------

@pytest.mark.parametrize("q", [
    "1420 Locust #23H", "1420 locust st 23h", "1420 Locust St Unit 23H", "1420 LOCUST STREET APT 23H",
    "1420 locust 23-H", "1420 Locust St., #23h", "  1420   locust   st   unit 23 h ", "1420 locust st suite 23H",
])
def test_unit_formats_all_parse_to_the_same_unit(q):
    pq = parse_query(q)
    assert pq.house_number == "1420"
    assert pq.unit == "23H"
    assert "LOCUST" in pq.words


@pytest.mark.parametrize("raw,expected", [
    ("#23H", "23H"), ("#UNIT 1R", "1R"), ("#APT. 2", "2"), ("#SUITE 6", "6"), ("23-h", "23H"), ("#4-B", "4B"),
])
def test_norm_unit(raw, expected):
    assert norm_unit(raw) == expected


@pytest.mark.parametrize("typed,canonical", [
    ("street", "ST"), ("St", "ST"), ("lane", "LN"), ("Ln", "LN"), ("drive", "DR"), ("road", "RD"),
    ("avenue", "AVE"), ("ave", "AVE"), ("boulevard", "BLVD"), ("court", "CT"),
])
def test_suffix_spellings_are_canonical(typed, canonical):
    assert parse_query(f"12 elm {typed}").words == ["ELM", canonical]


def test_ordinal_street_is_not_a_unit_but_a_trailing_unit_is():
    pq = parse_query("210 S 12th St 29D")
    assert pq.words == ["S", "12TH", "ST"]
    assert pq.unit == "29D"


def test_route_number_is_street_not_unit():
    pq = parse_query("100 route 30")
    assert pq.unit is None
    assert "30" in pq.words


def test_zip_and_town_after_street_are_words_not_units():
    pq = parse_query("417 Dogwood Ln, Coatesville PA 19320")
    assert pq.unit is None
    assert pq.words == ["DOGWOOD", "LN", "COATESVILLE", "PA", "19320"]


def test_five_digit_house_number():
    assert parse_query("14720 locust ct").house_number == "14720"


def test_city_alias_philly():
    assert parse_query("1420 locust philly").words[-1] == "PHILADELPHIA"


def test_half_typed_suffix_is_not_required_in_sql():
    pq = parse_query("417 dogwood la")
    assert pq.hard_words == ["DOGWOOD"]
    sql, params = build_candidate_sql(pq, 50)
    assert params["hn"] == "417 %"
    assert not any(v == "%LA%" for v in params.values())


def test_like_wildcards_typed_never_reach_the_sql_pattern():
    _, params = build_candidate_sql(parse_query("12 elm%_st"), 10)
    patterns = [v for v in params.values() if isinstance(v, str)]
    assert all("_" not in p and "%" not in p.strip("%").replace(" %", "") for p in patterns)


# ---- matching ----------------------------------------------------------------------------------

def test_typed_unit_matches_stored_unit_regardless_of_suffix():
    pq = parse_query("1420 Locust #23H")
    s = score_candidate(pq, cand("1420 LOCUST ST #23H"))
    assert s.hard_missing == 0 and s.unit_tier == 0 and s.house_exact


def test_stored_address_without_suffix_still_matches_typed_suffix():
    pq = parse_query("1420 locust st 10p")
    s = score_candidate(pq, cand("1420 LOCUST #10P"))
    assert s.hard_missing == 0 and s.unit_tier == 0 and s.soft_missing == 1


def test_street_vs_st_and_lane_vs_ln_match():
    assert score_candidate(parse_query("1420 Locust Street"), cand("1420 LOCUST ST")).hard_missing == 0
    assert score_candidate(parse_query("417 dogwood lane"), cand("417 DOGWOOD LN", city="COATESVILLE")).soft_missing == 0


def test_city_or_zip_after_street_matches():
    c = cand("417 DOGWOOD LN", city="COATESVILLE", zipcode="19320")
    assert score_candidate(parse_query("417 dogwood coatesville"), c).hard_missing == 0
    assert score_candidate(parse_query("417 dogwood 19320"), c).hard_missing == 0
    assert score_candidate(parse_query("417 dogwood coates"), c).hard_missing == 0  # still typing


def test_typo_is_a_near_match_not_a_miss():
    s = score_candidate(parse_query("239 windam"), cand("239 WINDHAM DR", city="EXTON"))
    assert s.hard_missing == 0 and s.fuzzy == 1


def test_wrong_house_number_is_a_miss():
    assert score_candidate(parse_query("1421 locust"), cand("1420 LOCUST ST")).hard_missing == 1


# ---- ranking + one row per home ----------------------------------------------------------------

def test_exact_unit_ranks_first_even_when_sold_and_other_units_are_active():
    pq = parse_query("1420 Locust #23H")
    rows = [cand(f"1420 LOCUST ST #{u}", status="ACTIVE-BRIGHT") for u in ("29C", "24M", "420", "14P", "12K", "6M")]
    rows.append(cand("1420 LOCUST ST #23H", key="sold", status="CLOSED-BRIGHT", modified=(2026, 7, 22)))
    rows.append(cand("1420 LOCUST ST #23H", key="rent", status="ACTIVE-BRIGHT", ptype="Residential Lease"))
    hits = group_and_pick(pq, rows)
    assert hits[0].record.listing_key == "sold"  # the sale, not the active rental
    assert hits[0].others == 1
    assert sum(1 for h in hits if h.record.unit == "23H") == 1  # one row for the home


def test_exact_house_and_street_beat_a_similar_street():
    pq = parse_query("1420 locust")
    hits = group_and_pick(pq, [cand("1420 LOCUSTWOOD DR", key="b"), cand("1420 LOCUST ST", key="a", status="CLOSED-BRIGHT")])
    assert hits[0].record.listing_key == "a"


def test_pick_record_prefers_active_sale_then_latest_sale():
    active = cand("5 ELM ST", key="active", status="ACTIVE-BRIGHT", modified=(2025, 1, 1))
    old_sale = cand("5 ELM ST", key="old", status="CLOSED-BRIGHT", modified=(2019, 1, 1))
    new_sale = cand("5 ELM ST", key="new", status="CLOSED-BRIGHT", modified=(2024, 1, 1))
    rental = cand("5 ELM ST", key="rent", status="ACTIVE-BRIGHT", ptype="Residential Lease", modified=(2026, 1, 1))
    assert pick_record([old_sale, rental, active, new_sale]).listing_key == "active"
    assert pick_record([old_sale, rental, new_sale]).listing_key == "new"


def test_rental_only_home_is_kept_and_flagged():
    pq = parse_query("210 s 12th st 29d")
    hits = group_and_pick(pq, [cand("210 S 12TH ST #29D", ptype="Residential Lease")])
    assert len(hits) == 1 and hits[0].rental_only


def test_rentals_sort_after_sales_and_are_capped_on_broad_searches():
    pq = parse_query("1420 locust")
    rows = [cand(f"1420 LOCUST ST #{i}R", ptype="Residential Lease") for i in range(5)]
    rows.append(cand("1420 LOCUST ST #9S", key="sale", status="CLOSED-BRIGHT"))
    hits = group_and_pick(pq, rows)
    assert hits[0].record.listing_key == "sale"
    assert sum(h.rental_only for h in hits) == 2


def test_unrelated_streets_are_dropped_when_nothing_matches():
    pq = parse_query("locsut st philadelphia")
    assert group_and_pick(pq, [cand("5944 WALNUT ST"), cand("4938 CHESTNUT ST")]) == []


def test_rental_note_text():
    assert RENTAL_NOTE == "Rental listing - a CMA values a sale"
