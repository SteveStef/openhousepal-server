"""The Wilmington PROOF article (written on live after the 11/16 evening push) still showed two things:
- the payment card added the $28 townhome fee although only 36 of 240 townhomes report one. The "at least half" rule
  was live, but it never knew the 240: the count of condos / townhomes for sale was read from the wrong position in
  the listing row (the longitude) and was always 0;
- the card said "481 homes for sale" where the article said 482 - the card reads the listings a moment later."""
import asyncio

from app.services.ai import ai_blog_pipeline_service as pipe
from app.services.areas import market_stats_service as ms


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalar(self):
        return None

    def first(self):
        return None


class _Db:
    """Answers the first query (the homes for sale) with the rows given, and every later one with nothing."""
    def __init__(self, rows):
        self.rows, self.calls = rows, 0

    async def execute(self, _statement):
        self.calls += 1
        return _Result(self.rows if self.calls == 1 else [])


def _row(price, home_type, fee=None, fee2=None):
    # the select's own order: price, lat, lng, home_type, tax, tax_year, fee, freq, levels, senior, fee2, freq2, includes, structure
    return (price, 39.74, -75.55, home_type, 2500, 2025, fee, "Monthly" if fee else None, ["2"], None, fee2, "Monthly" if fee2 else None, [], None)


def _cost(rows, monkeypatch):
    async def no_global(_key, _make):
        return None

    async def a_year(_db, _end, days):
        return days
    from sqlalchemy import true
    monkeypatch.setattr(ms, "_global", no_global)
    monkeypatch.setattr(ms, "covered_days", a_year)
    monkeypatch.setattr(ms.geography, "location_filter", lambda _model, _scope: true())
    return asyncio.run(ms.get_area_cost_profile(_Db(rows), {"city": "Wilmington", "state": "DE"}))


def test_the_count_of_townhomes_for_sale_is_really_counted(monkeypatch):
    rows = ([_row(250_000 + n, "TOWNHOUSE", fee=28) for n in range(36)] + [_row(240_000 + n, "TOWNHOUSE") for n in range(203)] + [_row(None, "TOWNHOUSE")]
            + [_row(200_000 + n, "CONDO", fee2=636) for n in range(53)] + [_row(415_000 + n, "SINGLE_FAMILY") for n in range(159)]
            + [_row(300_000 + n, "MULTI_FAMILY") for n in range(30)])
    cost = _cost(rows, monkeypatch)
    assert cost["hoa"]["townhouse"]["homes"] == 36 and cost["hoa"]["townhouse"]["of"] == 240          # was 0
    assert cost["hoa"]["condo"]["homes"] == 53 and cost["hoa"]["condo"]["of"] == 53
    assert cost["mix"]["homes"] == len(rows) == 482 and cost["mix"]["attached"]["count"] == 293 == 240 + 53
    # so the rule can work: 36 of 240 is not typical, 53 of 53 is
    fees = pipe.example_fees(cost)
    assert "townhouse" not in fees and fees["condo"]["monthly"] == 636
    # near the card's price most homes are townhomes -> the card adds no fee at all
    entry = ms.entry_profile([(r[0], r[4], r[3], r[13], r[5]) for r in rows])
    assert entry["near_townhomes"] > entry["near_condos"]
    card = pipe.payment_example(entry, {"rate_30": 7.11, "as_of": "2026-11-12"}, ["Wilmington"], fees=fees)
    assert card["fee"] is None and all("with_fee" not in s for s in card["scenarios"])


def test_the_card_states_the_article_s_own_count_of_homes_for_sale():
    rows = [(200_000 + 1_000 * n, 2_000, "TOWNHOUSE", None, 2025) for n in range(481)]
    entry = ms.entry_profile(rows)
    rate = {"rate_30": 7.11, "as_of": "2026-11-12"}
    assert "481 homes for sale" in pipe.payment_example(entry, rate, ["Wilmington"])["price_basis"]
    # the article's snapshot, taken a moment earlier, said 482: the card says 482 too
    assert "482 homes for sale" in pipe.payment_example(entry, rate, ["Wilmington"], homes_for_sale=482)["price_basis"]
    # a count that is not the same homes (far apart) is never borrowed
    assert "481 homes for sale" in pipe.payment_example(entry, rate, ["Wilmington"], homes_for_sale=530)["price_basis"]
    assert "481 homes for sale" in pipe.payment_example(entry, rate, ["Wilmington"], homes_for_sale=0)["price_basis"]
