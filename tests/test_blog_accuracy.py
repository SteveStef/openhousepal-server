"""Blog engine accuracy guards (10/24 review of the Main Line first-time-buyer article): the
geography audit, the research scrubber, the loan-type lint, the payment-example math, source
links, and what market snapshot items say. Pure functions - no model, no database."""
from types import SimpleNamespace

from app.services.ai import ai_blog_pipeline_service as pipe
from app.services.ai import ai_blog_writer_service as writer
from app.services.areas import town_knowledge_service as tk


def _profile(town, kind, townships, districts, counties):
    return {
        "town": town, "kind": kind, "townships": [{"name": t} for t in townships], "school_districts": [{"name": d} for d in districts],
        "counties": counties, "mailing_towns": [],
        "norm": {"townships": {tk.norm_place(t) for t in townships}, "districts": {tk.norm_place(d) for d in districts},
                 "counties": {tk.norm_place(c) for c in counties}, "mailing": set()},
    }


WAYNE = _profile("Wayne", "postal town", ["Radnor Township", "Tredyffrin Township", "Upper Merion Township"],
                 ["Radnor Township School District", "Tredyffrin-Easttown School District", "Upper Merion Area School District"],
                 ["DELAWARE-PA", "CHESTER-PA", "MONTGOMERY-PA"])
VILLANOVA = _profile("Villanova", "postal town", ["Lower Merion Township", "Radnor Township"],
                     ["Lower Merion School District", "Radnor Township School District"], ["MONTGOMERY-PA", "DELAWARE-PA"])
RADNOR = _profile("Radnor", "township", ["Radnor Township"], ["Radnor Township School District"], ["DELAWARE-PA"])
PROFILES = [WAYNE, VILLANOVA, RADNOR]
KNOWN = {"townships": {"lower merion", "radnor", "tredyffrin", "upper merion", "haverford", "easttown"}, "counties": {"delaware", "chester", "montgomery"}}


def flagged(text):
    return {(f["town"], f["place"].lower()) for f in tk.geography_audit(text, PROFILES, KNOWN)}


def test_norm_place():
    assert tk.norm_place("Tredyffrin-Easttown School District") == "tredyffrin easttown"
    assert tk.norm_place("UPPER MERION AREA") == "upper merion"
    assert tk.norm_place("Lower Merion Township") == "lower merion"
    assert tk.norm_place("DELAWARE-PA") == "delaware"


def test_audit_catches_the_published_error():
    assert flagged("Villanova and Wayne lie partly in Lower Merion and partly in Radnor, so I confirm the municipality.") == {("Wayne", "lower merion")}


def test_audit_passes_true_geography():
    for ok in (
        "Villanova lies partly in Lower Merion and partly in Radnor Township.",
        "Wayne spans Radnor, Tredyffrin and Upper Merion townships.",
        "Some Wayne homes are in the Tredyffrin-Easttown School District.",
        "A Wayne address can be in Radnor, Tredyffrin or Upper Merion Township, served by the Radnor Township, Tredyffrin-Easttown or Upper Merion Area school districts.",
        "Parts of Wayne are in Chester County and parts in Montgomery County.",
        "Radnor Township includes parts of Wayne and Villanova.",
        "Wayne has a lively main street.",
    ):
        assert flagged(ok) == set(), ok


BRYN_MAWR = _profile("Bryn Mawr", "postal town", ["Radnor Township", "Lower Merion Township", "Haverford Township"],
                     ["Radnor Township School District", "Lower Merion School District", "Haverford Township School District"],
                     ["DELAWARE-PA", "MONTGOMERY-PA"])


def test_audit_pairs_each_place_with_its_own_town():
    profiles = [BRYN_MAWR, WAYNE]
    ok = ("A Bryn Mawr home in Lower Merion Township and a Wayne home in Tredyffrin Township are in two different townships, "
          "and they're served by different school districts.")
    assert tk.geography_audit(ok, profiles, KNOWN) == []
    bad = "Wayne and Bryn Mawr both lie partly in Lower Merion."
    assert {(f["town"], f["place"].lower()) for f in tk.geography_audit(bad, profiles, KNOWN)} == {("Wayne", "lower merion")}


def test_audit_catches_wrong_district_county_and_township():
    assert ("Wayne", "lower merion") in flagged("Some Wayne addresses fall in the Lower Merion School District.")
    assert ("Villanova", "chester") in flagged("Villanova homes sit in Chester County.")
    assert ("Villanova", "haverford") in flagged("Villanova is in Haverford Township.")


def test_scrub_keeps_the_fact_and_drops_the_aside():
    items = [{"claim": "Lower Merion approved an 8% tax increase for 2026. Villanova and Wayne lie partly in Lower Merion and Radnor, so confirm each property.",
              "origin": "evidence_bank"}, {"claim": "Wayne lies in Lower Merion.", "origin": "web_research"},
             {"claim": "Wayne lies in Lower Merion.", "origin": "geo_verified"}]
    out, removed = tk.scrub_geography(items, PROFILES, KNOWN)
    assert removed == 2
    assert out[0]["claim"] == "Lower Merion approved an 8% tax increase for 2026."
    assert len(out) == 2 and out[1]["origin"] == "geo_verified"  # verified items are never touched


def test_loan_rule_lint():
    bad = "Borrowers who put down less than 20% must also get mortgage insurance."
    good = "On a conventional loan, a down payment under 20% generally means PMI. VA loans need no mortgage insurance."
    assert pipe.loan_rule_lint(bad) == [bad]
    assert pipe.loan_rule_lint(good) == []


def test_payment_example_math():
    ex = pipe.payment_example({"p25": 547_300, "homes": 99, "tax_pct_near_p25": 1.21}, {"rate_30": 7.40, "as_of": "2026-10-15"},
                              ["Villanova", "Radnor", "Wayne"], conforming_limit=832_750)
    assert ex["price"] == 550_000
    s20 = next(s for s in ex["scenarios"] if s["down_pct"] == 20)
    assert (s20["down"], s20["loan"], s20["principal_interest"], s20["mortgage_insurance"]) == (110_000, 440_000, 3046, "none")
    assert s20["tax"] == round(550_000 * 0.0121 / 12)
    assert next(s for s in ex["scenarios"] if s["down_pct"] == 5)["mortgage_insurance"] == "lender quote"
    assert (ex["closing_low"], ex["closing_high"]) == (11_000, 27_500)
    assert not any(s["jumbo"] for s in ex["scenarios"])
    big = pipe.payment_example({"p25": 1_200_000, "homes": 40}, {"rate_30": 7.0, "as_of": "2026-10-15"}, ["Villanova"], conforming_limit=832_750)
    assert next(s for s in big["scenarios"] if s["down_pct"] == 10)["jumbo"] is True
    assert pipe.payment_example({}, {"rate_30": 7.0}, []) is None


def test_payment_example_trigger():
    assert pipe.wants_payment_example({"topic": "Main Line first-time homebuyer guide"})
    assert not pipe.wants_payment_example({"topic": "Best parks in Wayne", "audiences": ["Locals"]})


def test_inline_links_only_to_known_sources():
    allowed = {"https://www.phfa.org/programs/assistance/keystone-advantage.aspx"}
    html = writer._rich("See [PHFA's Keystone Advantage](https://www.phfa.org/programs/assistance/keystone-advantage.aspx) and [this blog](https://spam.example.com/x).", allowed)
    assert '<a href="https://www.phfa.org/programs/assistance/keystone-advantage.aspx" target="_blank" rel="noopener">PHFA\'s Keystone Advantage</a>' in html
    assert "spam.example.com" not in html and "this blog" in html
    assert "https://" not in writer._rich("[x](https://www.phfa.org/programs/assistance/keystone-advantage.aspx)")  # no allow-list = no links


def test_snapshot_items_say_what_they_cover():
    area = SimpleNamespace(name="Wayne", state="PA", municipality_type=None, area_type="city")
    snap = {"window_days": 90, "data_through": "2026-10-16", "prior_year_reliable": False, "inventory_as_of": "2026-10-17",
            "current": {"closed_count": 8, "median_close_price": 990_000, "median_sale_to_list_pct": 100.0, "median_days_on_market": 8},
            "prior_year": {"closed_count": 0}, "active_count": 46, "active_median_list_price": 842_000, "active_p25_list_price": 400_000,
            "under_contract_count": 28}
    claims = " ".join(i["claim"] for i in tk.sales_snapshot_items(area, snap, WAYNE))
    assert "Definition - Wayne numbers cover homes with a Wayne mailing address, which spans Radnor Township, Tredyffrin Township, Upper Merion Township" in claims
    assert "46 homes for sale (active listings)" in claims and "28 are already under contract or pending" in claims
    assert "small sample" in claims
    assert "(vs " not in claims and "year-ago comparisons are left out" in claims


def test_federal_items_cover_loan_types():
    text = " ".join(i["claim"] for i in tk.federal_items())
    for needle in ("Conventional loans", "FHA", "VA-backed", "$832,750", "2-5%"):
        assert needle in text
    assert all(i["source_url"].startswith("https://") for i in tk.federal_items())
