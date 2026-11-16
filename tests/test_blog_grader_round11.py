"""Grader round 11 (11/16) - two proof articles written on the LIVE site after the round 10 fixes. Scores 86 and 92,
no confirmed factual error. Three things to settle:

  1. Radnor Township's Real Estate Transfers page says the occupancy-certificate application goes in "no less than 10
     business days prior to settlement"; its code (section 142-4) says 30 days, or 14 for a quick settlement, and a
     different fee. The live store held BOTH and the article printed the page's number, because "two readings of the
     same page agreed" never looked at what the municipality's other official page says. A number its own pages
     disagree about is now never stated, however often either page was read.
  2. The article quoted a $9,583 median tax bill for 32 listings while the card showed about $5,892 a year on its
     $460,000 example "using" the same 32 bills. Right, but unexplained: the card scales each bill to its home's price.
     It now says so.
  3. A source link spelled https://wwww.septa.org/fares/ (it works: the site answers on any name).

No AI, no database: plain functions."""
from datetime import datetime, timedelta, timezone

from app.services.ai import ai_blog_pipeline_service as pipe
from app.services.areas import town_knowledge_service as tk

PAGE = "https://www.radnor.com/government/departments/engineering/real-estate-transfers"
CODE = "https://ecode360.com/10836471"
T0 = datetime(2026, 11, 15, 22, 59, tzinfo=timezone.utc)


def _item(claim, url, saved=T0, reads=()):
    return {"claim": claim, "source_url": url, "municipality": "Radnor Township", "saved_at": saved.isoformat(), "reads": [r.isoformat() for r in reads]}


def _radnor():
    """What the live store held on 11/16: the township page read on two separate runs, its code read once."""
    return [
        _item("Per the township page: the seller files the certificate of use and occupancy application with the Engineering Department no less than "
              "10 business days prior to settlement (a filing deadline); a $300 fee is charged for an expedited application made 10 days or less "
              "before settlement. The application fee is $150.", PAGE, reads=[T0 + timedelta(hours=6)]),
        _item("Radnor Township Code §142-4: the seller files the certificate of use and occupancy application not later than 30 days before settlement, "
              "or not less than 14 days before settlement if settlement is scheduled less than 30 days after the sale agreement. A fee of $100 "
              "accompanies the application.", CODE),
    ]


def test_a_number_the_townships_own_pages_disagree_about_is_never_confirmed():
    items = _radnor()
    disputed = tk.disputed_specifics(items)
    assert {"10 day", "30 day", "14 day"} <= disputed                 # the lead times
    assert {"$100", "$150", "$300"} <= disputed                       # and the fees
    # two readings of the page used to make "10 day" sure - not while the code says otherwise
    assert "10 day" not in tk.confirmed_specifics(items)
    # without the code's reading the old rule still holds: two separate readings of one page confirm its number
    assert "10 day" in tk.confirmed_specifics(items[:1])
    assert tk.disputed_specifics(items[:1]) == set()


def test_the_writer_sees_no_number_and_is_told_the_pages_differ():
    items = _radnor()
    sure, disputed = tk.confirmed_specifics(items), tk.disputed_specifics(items)
    shown, held = tk.mask_unconfirmed(items[0]["claim"], sure, disputed)
    assert held >= 3 and "10 business days" not in shown and "$300" not in shown
    assert "DIFFERENTLY" in shown and "do NOT state a number" in shown
    # the article check that catches a held-back number finds this note too (same marker words) ...
    ledger = [{"claim": shown, "source_url": PAGE}]
    text = "The township asks the seller to file the application at least 10 business days before settlement."
    assert [n for _s, n in pipe.unconfirmed_number_claims(text, ledger)] == ["10 business days"]
    # ... and the writer's rules say what to write instead
    assert any("give it DIFFERENTLY" in " ".join(x for x in row if isinstance(x, str)) for row in pipe._COVERAGE)


def test_the_same_deadline_said_two_ways_is_not_a_dispute():
    form = "https://www.radnor.com/forms/co-application.pdf"
    same = [_item("The page says the occupancy certificate application is due no less than 10 business days prior to settlement.", PAGE),
            _item("The application form says to apply for the certificate of occupancy at least two weeks before settlement.", form)]
    assert tk.disputed_specifics(same) == set()
    assert tk._calendar_days("10 business days") == tk._calendar_days("two weeks") == 14 and tk._calendar_days("1 month") == tk._calendar_days("30 days")
    # a page that gives one of the other page's two deadlines is only shorter, not in dispute
    fuller = [_item("The occupancy certificate application is due 30 days before settlement, or 14 days before settlement for a quick sale.", CODE),
              _item("Apply for the certificate of occupancy at least 30 days before settlement.", PAGE)]
    assert tk.disputed_specifics(fuller) == set()
    # two different requirements never dispute each other
    apart = [_item("The use and occupancy certificate application is due 30 days before settlement.", PAGE),
             _item("The sewer lateral inspection must be scheduled 10 days before settlement.", "https://www.radnor.com/public-works/sewer")]
    assert tk.disputed_specifics(apart) == set()
    # a day count that is not a filing lead time (how long the township takes) is not compared
    speed = [_item("The occupancy certificate application is due 30 days before settlement.", PAGE),
             _item("The township inspects within 5 business days of a complete occupancy certificate application.", CODE)]
    assert tk.disputed_specifics(speed) == set()


def test_a_disputed_number_is_no_reason_to_read_the_page_again():
    # every number on both pages is in dispute: another reading of either settles nothing, so none is asked for
    assert tk.unconfirmed_pages(_radnor()) == []
    # a page with an ordinary unconfirmed number is still worth one more reading
    once = [_item("The occupancy certificate application is due 20 days before settlement.", PAGE)]
    assert [p["url"] for p in tk.unconfirmed_pages(once)] == [PAGE]


def test_the_card_says_its_tax_estimate_is_scaled_to_the_price():
    assert pipe.tax_basis_words("entry", 32, "all") == "tax bills of the 32 homes for sale that list one, scaled to this price"
    assert pipe.tax_basis_words("house", 9, "near").endswith("similar-priced single-family houses for sale, scaled to this price")
    # the arithmetic it describes: the middle bill-to-price share of those homes, applied to the example's price
    rows = [(400_000 + 100_000 * n, (400_000 + 100_000 * n) * 0.0128, "SINGLE_FAMILY", "Detached", "2026") for n in range(12)]
    from app.services.areas import market_stats_service as ms
    entry = ms.entry_profile(rows)
    assert entry["tax_pct_near_p25"] == 1.28
    ex = pipe.payment_example(entry, {"rate_30": 7.11, "as_of": "2026-11-12"}, ["Radnor"], basis="entry")
    assert ex and "scaled to this price" in ex["tax_basis"]


def test_a_source_link_with_one_w_too_many_is_tidied():
    assert pipe.tidy_url("https://wwww.septa.org/fares/") == "https://www.septa.org/fares/"
    assert pipe.tidy_url(" https://WWWWW.septa.org/x ") == "https://www.septa.org/x"
    for fine in ("https://www.septa.org/fares/", "https://schedules.septa.org/current/PAO.pdf", "https://wwwwonderland.example.org/a", "https://ecode360.com/10836471"):
        assert pipe.tidy_url(fine) == fine
    ledger, _text = pipe._build_ledger([{"claim": "SEPTA lists its fares by zone.", "source_url": "https://wwww.septa.org/fares/", "source_name": "SEPTA", "origin": "state_bank"}])
    assert ledger[0]["source_url"] == "https://www.septa.org/fares/"
