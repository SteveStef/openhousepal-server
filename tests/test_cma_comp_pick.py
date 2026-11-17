"""Which comps the Comps step picks for the agent (cma_comp_pick, 11/16): Match score, plus - for a listed home - the
price level of each sale, with the school-district rule and the nearer-the-asking-price check."""
from types import SimpleNamespace

import pytest

from app.services import cma_ai_service  # noqa: F401  (loaded first, as the app does at start-up)
from app.services import cma_comp_pick as cp


def _c(key, score, psf, *, district="Upper Darby", city="Drexel Hill", sqft=2000, year=1946, details=None):
    return SimpleNamespace(listing_key=key, match_score=score, sale_price=psf * sqft, sqft=sqft, above_grade_finished_area=sqft,
                           school_district=district, city=city, year_built=year, similarity_details=details or {}, property_type="SINGLE_FAMILY")


SUBJECT = {"school_district": "Upper Darby", "city": "Drexel Hill", "year_built": 1946, "sqft": 2000}
ASK = 245 * 2000          # the subject asks $245 a square foot


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(cp, "PRICE_LEVEL_POINTS", 6.0)
    monkeypatch.setattr(cp, "TOWN_COUNTS_AS_OUTSIDE", False)
    # the sales figure of a set = the average sold $/sq ft x the subject's size (stands in for the adjustment table)
    monkeypatch.setattr(cp, "sales_figure", lambda subject, picks: sum(c.sale_price / c.sqft for c in picks) / len(picks) * 2000)


def _pool():
    return [_c("dated1", 85, 162), _c("dated2", 84, 150), _c("nice1", 86, 240), _c("nice2", 83, 250), _c("nice3", 82, 232),
            _c("nice4", 80, 255), _c("mid", 81, 205)]


def test_with_no_asking_price_the_pick_is_the_plain_match_score_pick():
    out = cp.suggest(SUBJECT, _pool(), asking_price=None)
    assert out["keys"] == ["nice1", "dated1", "dated2", "nice2", "nice3"] and out["price_level"] is False and out["reason"] == "no asking price"
    assert cp.suggest({**SUBJECT, "sqft": None}, _pool(), asking_price=ASK)["reason"] == "no asking price"   # no size to work a $/sq ft from


def test_a_listed_home_gets_sales_at_its_own_price_level_and_the_match_scores_are_untouched():
    pool = _pool()
    out = cp.suggest(SUBJECT, pool, asking_price=ASK)
    assert out["price_level"] is True and set(out["keys"]) == {"nice1", "nice2", "nice3", "nice4", "mid"}
    assert "dated1" not in out["keys"] and "dated2" not in out["keys"]       # $162 and $150 a foot against $245
    assert [c.match_score for c in pool] == [85, 84, 86, 83, 82, 80, 81]     # the score shown to people never changes


def test_the_first_ten_percent_of_price_gap_costs_nothing():
    pool = [_c("a", 80, 245), _c("b", 79, 225), _c("c", 78, 268), _c("d", 77, 245), _c("e", 76, 245)]     # all within ~10%
    out = cp.suggest(SUBJECT, pool, asking_price=ASK)
    assert out["keys"] == ["a", "b", "c", "d", "e"] and out["price_level"] is False and out["reason"] == "same homes either way"


def test_a_sale_from_another_school_district_is_never_brought_in_by_the_price_level():
    # three dated homes in the district match best on paper; three at the right price level sit just outside it.
    # On price level alone the three outside would be the pick (59 > 56) - the rule keeps them out.
    pool = [_c("dated1", 75, 162), _c("dated2", 74, 150), _c("dated3", 73, 158), _c("nice_far", 59, 245, district="Haverford Township"),
            _c("nice_far2", 58, 245, district="Haverford Township"), _c("nice_far3", 57, 245, district="Haverford Township")]
    out = cp.suggest(SUBJECT, pool, asking_price=ASK)
    assert out["keys"] == ["dated1", "dated2", "dated3"] and out["price_level"] is False
    # ... but one the plain pick already had may stay
    pool = [_c("nice_far", 90, 245, district="Haverford Township"), _c("dated1", 85, 162), _c("dated2", 84, 150), _c("nice1", 70, 245), _c("nice2", 69, 245)]
    out = cp.suggest(SUBJECT, pool, asking_price=ASK)
    assert "nice_far" in out["keys"] and out["keys"][:1] == ["nice_far"]


def test_another_town_in_the_same_district_is_fine_unless_the_town_rule_is_on(monkeypatch):
    pool = [_c("dated1", 75, 162), _c("dated2", 74, 150), _c("dated3", 73, 158), _c("t1", 59, 245, city="Clifton Heights"),
            _c("t2", 58, 245, city="Clifton Heights"), _c("t3", 57, 245, city="Clifton Heights")]
    monkeypatch.setattr(cp, "sales_figure", lambda subject, picks: 0.9 * ASK if any(c.listing_key == "dated1" for c in picks) else ASK)
    assert set(cp.suggest(SUBJECT, pool, asking_price=ASK)["keys"]) == {"t1", "t2", "t3"}
    monkeypatch.setattr(cp, "TOWN_COUNTS_AS_OUTSIDE", True)
    assert set(cp.suggest(SUBJECT, pool, asking_price=ASK)["keys"]) == {"dated1", "dated2", "dated3"}


def test_the_new_pick_is_kept_only_when_its_sales_end_nearer_the_asking_price(monkeypatch):
    pool = _pool()
    monkeypatch.setattr(cp, "sales_figure", lambda subject, picks: 480000 if any(c.listing_key == "dated1" for c in picks) else 400000)
    out = cp.suggest(SUBJECT, pool, asking_price=ASK)                         # plain pick $480K vs the new one $400K; asking $490K
    assert out["price_level"] is False and "dated1" in out["keys"] and "nearer the asking price" in out["reason"]
    monkeypatch.setattr(cp, "sales_figure", lambda subject, picks: None)      # no figure for a set: the plain pick stands
    assert cp.suggest(SUBJECT, pool, asking_price=ASK)["price_level"] is False


def test_an_asking_price_far_from_the_sales_does_not_get_to_choose_the_comps(monkeypatch):
    # 1459 N Hirst St: asked $135,000; the plain pick's sales said $105,000 (22% under). Steering toward the price hid that.
    pool = _pool()
    monkeypatch.setattr(cp, "sales_figure", lambda subject, picks: 0.78 * ASK if any(c.listing_key == "dated1" for c in picks) else 0.98 * ASK)
    out = cp.suggest(SUBJECT, pool, asking_price=ASK)
    assert out["price_level"] is False and "dated1" in out["keys"] and "too far from the asking price" in out["reason"]
    monkeypatch.setattr(cp, "sales_figure", lambda subject, picks: 0.85 * ASK if any(c.listing_key == "dated1" for c in picks) else 0.98 * ASK)
    assert cp.suggest(SUBJECT, pool, asking_price=ASK)["price_level"] is True          # 15% away: still inside the limit


def test_it_can_be_switched_off_and_never_breaks_the_comp_step(monkeypatch):
    pool = _pool()
    monkeypatch.setattr(cp, "PRICE_LEVEL_POINTS", 0.0)
    assert cp.suggest(SUBJECT, pool, asking_price=ASK) == {"keys": ["nice1", "dated1", "dated2", "nice2", "nice3"], "price_level": False, "reason": "switched off"}
    monkeypatch.setattr(cp, "PRICE_LEVEL_POINTS", 6.0)

    def boom(subject, picks):
        raise RuntimeError("table failed")
    monkeypatch.setattr(cp, "sales_figure", boom)
    out = cp.suggest(SUBJECT, pool, asking_price=ASK)
    assert out["keys"] == ["nice1", "dated1", "dated2", "nice2", "nice3"] and out["reason"] == "error"


def test_sales_that_cannot_be_picked_stay_out_and_the_fourth_and_fifth_need_sixty():
    pool = [_c("a", 90, 245), _c("band", 89, 245, details={"outside_price_band": True}), _c("type", 88, 245, details={"property_type_incompatible": True}),
            _c("b", 70, 245), _c("c", 55, 245), _c("d", 54, 245), _c("e", 53, 245)]
    assert cp.suggest(SUBJECT, pool, asking_price=None)["keys"] == ["a", "b", "c"]          # top three always, then 60+ only


def test_out_of_the_box_the_pick_is_the_highest_match_scores_and_nothing_else(monkeypatch):
    """Owner 11/16: "the higher scores should be shown in highest to lowest and highest auto selected"."""
    import importlib
    monkeypatch.delenv("CMA_PICK_PRICE_POINTS", raising=False)
    fresh = importlib.reload(cp)
    try:
        assert fresh.PRICE_LEVEL_POINTS == 0
        out = fresh.suggest(SUBJECT, _pool(), asking_price=ASK)
        assert out == {"keys": ["nice1", "dated1", "dated2", "nice2", "nice3"], "price_level": False, "reason": "switched off"}   # 86, 85, 84, 83, 82
    finally:
        importlib.reload(cp)


# ---- Price level inside the Match % itself (owner 11/17: "move this into the score in a safe way") ----

class _Cand(SimpleNamespace):
    def model_copy(self, update=None):
        return _Cand(**{**self.__dict__, **(update or {})})


def _m(key, score, psf, *, district="Upper Darby", sold=True):
    return _Cand(listing_key=key, match_score=score, sale_price=psf * 2000 if sold else None, sqft=2000, above_grade_finished_area=2000,
                 school_district=district, city="Drexel Hill", year_built=1946, similarity_details={"version": 1}, property_type="SINGLE_FAMILY")


def _scores(out):
    return {c.listing_key: c.match_score for c in out}


def test_a_sale_at_a_very_different_price_level_scores_lower_and_one_at_the_same_level_keeps_its_score(monkeypatch):
    monkeypatch.setattr(cp, "MATCH_PRICE_POINTS", 6.0)
    pool = [_m("dated", 85, 162), _m("same", 80, 245), _m("near", 78, 228), _m("active", 90, 0, sold=False)]
    out = cp.apply_price_level(SUBJECT, pool, asking_price=ASK)
    s = _scores(out)
    assert s == {"dated": 66, "same": 80, "near": 78, "active": 90}            # $162 a foot against $245 asked: 19 points
    d = next(c for c in out if c.listing_key == "dated").similarity_details
    assert d["price_level"] == {"points_off": 19, "match_before": 85, "held_under_district": False, "comp_price_per_sqft": 162, "asking_price_per_sqft": 245}
    assert d["display_match_score"] == 66 and d["version"] == 1
    assert pool[0].match_score == 85                                            # the objects passed in are not changed
    assert [c.listing_key for c in out] == ["dated", "same", "near", "active"]  # same order; the page sorts by score


def test_no_asking_price_or_switched_off_leaves_every_score_alone(monkeypatch):
    pool = [_m("dated", 85, 162), _m("same", 80, 245)]
    monkeypatch.setattr(cp, "MATCH_PRICE_POINTS", 6.0)
    assert cp.apply_price_level(SUBJECT, pool, asking_price=None) is pool
    assert cp.apply_price_level({**SUBJECT, "sqft": None}, pool, asking_price=ASK) is pool
    monkeypatch.setattr(cp, "MATCH_PRICE_POINTS", 0.0)
    assert cp.apply_price_level(SUBJECT, pool, asking_price=ASK) is pool


def test_a_sale_from_another_school_district_never_ends_up_ahead_of_an_in_district_one_it_trailed(monkeypatch):
    monkeypatch.setattr(cp, "MATCH_PRICE_POINTS", 6.0)
    far = "Haverford Township"
    pool = [_m("in_dated", 85, 162), _m("out_same", 80, 245, district=far), _m("in_same", 75, 245), _m("out_low", 60, 245, district=far)]
    s = _scores(cp.apply_price_level(SUBJECT, pool, asking_price=ASK))
    assert s["in_dated"] == 66 and s["out_same"] == 65 and s["in_same"] == 75 and s["out_low"] == 60
    # before: in_dated 85 > out_same 80. After: still in_dated 66 > out_same 65 - held just under it, not left at 80.
    out = cp.apply_price_level(SUBJECT, pool, asking_price=ASK)
    assert next(c for c in out if c.listing_key == "out_same").similarity_details["price_level"]["held_under_district"] is True
    # an outside sale that already led every in-district sale is not pulled down for it
    pool = [_m("out_top", 90, 245, district=far), _m("in_dated", 85, 162)]
    assert _scores(cp.apply_price_level(SUBJECT, pool, asking_price=ASK)) == {"out_top": 90, "in_dated": 66}


def test_the_first_ten_percent_is_free_and_trouble_means_plain_scores(monkeypatch):
    monkeypatch.setattr(cp, "MATCH_PRICE_POINTS", 6.0)
    pool = [_m("a", 80, 225), _m("b", 79, 268)]
    assert cp.apply_price_level(SUBJECT, pool, asking_price=ASK) is pool
    assert cp.price_points_off(245, 0, 2000) == 0 and cp.price_points_off(None, 400000, 2000) == 0 and cp.price_points_off(245, "x", 2000) == 0
    bad = [SimpleNamespace(listing_key="z", match_score=80, sale_price=300000, sqft=2000, above_grade_finished_area=2000, school_district=None, similarity_details=None)]
    assert cp.apply_price_level(SUBJECT, bad, asking_price=ASK) is bad            # no model_copy on it: plain scores, no crash


def test_both_search_routes_the_past_sales_tool_and_the_report_use_the_same_score():
    import inspect
    from app.api import cma_routes
    from app.services import cma_backtest_service, cma_ai_service as ai
    assert "apply_price_level(" in inspect.getsource(cma_routes.search_cma_comparables)
    assert "apply_price_level(" in inspect.getsource(cma_routes.auto_search_cma_comparables)
    assert "pick_keys" not in inspect.getsource(cma_routes.auto_search_cma_comparables)       # no second, hidden order
    assert "apply_price_level(" in inspect.getsource(cma_backtest_service.wizard_comps)
    body = inspect.getsource(ai._apply_continuous_relevance_weighting)
    assert '"match_before_price_level": _plain_match' in body and "_pl[\"points_off\"]" in body


def test_the_evidence_label_still_reads_the_plain_match_score():
    from app.services import cma_ai_service as ai
    assert ai._plain_match_score({"match_score": 66, "similarity_details": {"match_before_price_level": 85}}) == 85
    assert ai._plain_match_score({"match_score": 80}) == 80 and ai._plain_match_score({"match_score": 80, "similarity_details": None}) == 80
    assert ai._plain_match_score(None) is None
