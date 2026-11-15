"""Owner, 11/14: the writer's interview "is not intelligent sometimes ... I feel like I'm repeating myself ... I wish I was
selecting more than typing, based on suggestions the AI sees from the title". A second reviewer walked the flow: 12
questions, the five that matter are blank typing boxes, the "angle" ideas named the agent's first town ("what buyers in
Villanova should know" on a Wayne SELLER article), and the town and audience in the title were never ticked.

blog_suggest_service: free picks from the title in plain code + ONE small AI call for tappable choices."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.services.ai import blog_suggest_service as sug  # noqa: E402

TOWNS = ["Villanova", "Bryn Mawr", "Wayne", "Radnor", "Ardmore", "Media", "Old City"]


def test_the_title_ticks_the_reader_the_town_and_the_closing_button():
    a = sug.from_title("First-Time Homebuyer Guide to Conshohocken: Townhome or Condo?", preset="Buyers", known_towns=TOWNS)
    assert a["audiences"] == ["First-time buyers"] and a["areas"] == [] and a["cta"] == "buying" and a["length"] == "long"
    b = sug.from_title("Should I sell my Wayne home before or after the holidays?", known_towns=TOWNS)
    assert b["audiences"] == ["Sellers"] and b["areas"] == ["Wayne"] and b["cta"] == "home_value"       # never Villanova, never buyers
    c = sug.from_title("Bryn Mawr Market Update: November 2026", preset="Market Updates", known_towns=TOWNS)
    assert c["areas"] == ["Bryn Mawr"] and c["audiences"] == [] and c["cta"] == "how_can_i_help" and c["length"] == "short"
    d = sug.from_title("Selling the family home in Media and downsizing", known_towns=TOWNS)
    assert d["audiences"] == ["Sellers", "Homeowners"] and d["areas"] == ["Media"]
    # a word that only looks like a town is not one
    assert sug.from_title("Using social media to sell your home in the old city core", known_towns=TOWNS)["areas"] == []
    assert sug.from_title("Luxury homes in Villanova and Radnor", known_towns=TOWNS)["tone"] == "luxury"


def test_the_ai_choices_are_cleaned_and_the_town_must_really_be_in_the_title():
    title = "First-Time Homebuyer Guide to Conshohocken: Townhome or Condo?"
    base = sug.from_title(title, known_towns=TOWNS)
    ai = {"towns": ["Conshohocken", "Villanova"], "audiences": ["Buyers"],
          "angles": ["Conshohocken Townhome or Condo? A First-Time Buyer's Guide", "x" * 120],
          "goals": ["Know whether a condo or a townhome fits their budget", "Save $20,000 at closing"],
          "key_points": ["What you own in a condo vs a townhome", "Monthly association fees and what they cover", "Great for families with kids",
                         "Median price is $450,000", "What you own in a condo vs a townhome"],
          "takes": ["My take: read the association's budget before you fall for the kitchen.", "I've helped 40 buyers here.", "My take: perfect for young professionals"],
          "tone": "professional", "length": "long", "cta": "showing"}
    out = sug.merge(base, ai, title)
    assert out["areas"] == ["Conshohocken"]                                   # the model may not hand back a town from the agent's list
    assert out["audiences"] == ["First-time buyers"]                          # plain code's pick wins
    assert out["angles"] == ["Conshohocken Townhome or Condo? A First-Time Buyer's Guide"]
    assert out["goals"] == ["Know whether a condo or a townhome fits their budget"]             # no numbers in a choice
    assert out["key_points"] == ["What you own in a condo vs a townhome", "Monthly association fees and what they cover"]   # no fair-housing words, no facts, no repeats
    assert out["takes"] == ["My take: read the association's budget before you fall for the kitchen."]   # opinions only - never a track record
    assert out["cta"] == "buying" and out["ai"] is True and out["tone"] == "professional"


def test_without_the_ai_the_screen_still_gets_choices_that_fit_the_reader():
    title = "Should I sell my Wayne home before or after the holidays?"
    out = sug.merge(sug.from_title(title, known_towns=TOWNS), None, title)
    assert out["ai"] is False and out["areas"] == ["Wayne"] and out["angles"] == [] and out["takes"] == []
    assert "What a seller pays at settlement" in out["key_points"] and len(out["goals"]) == 2
    buyer = sug.merge(sug.from_title("Buying a condo in Ardmore", known_towns=TOWNS), None, "Buying a condo in Ardmore")
    assert "What the monthly payment looks like" in buyer["key_points"]


def test_one_small_metered_call_per_title_and_no_error_when_the_allowance_is_spent(monkeypatch):
    calls = {"ai": 0, "events": 0, "reconciled": []}

    class Rows:
        def all(self):
            return [("Wayne",), ("Villanova",)]

    class Db:
        async def execute(self, *_a, **_k):
            return Rows()

    async def config(_db):
        return {"copywrite_model": "claude-haiku-5-5"}

    async def reserve(*_a, **_k):
        return "reservation"

    async def event(*_a, **k):
        calls["events"] += 1
        assert k["event_type"] == "blog_writer" and k["tool_names"] == ["blog_suggest"]

    async def reconcile(_db, _r, cost):
        calls["reconciled"].append(cost)

    async def create(_client, **kw):
        calls["ai"] += 1
        assert kw["model"] == "claude-haiku-5-5" and "Should I sell my Wayne home" in kw["messages"][0]["content"]
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=700, output_tokens=600), content=[SimpleNamespace(
            type="tool_use", name="suggest_brief", input={"towns": ["Wayne"], "audiences": ["Sellers"], "angles": ["Sell Your Wayne Home Now or Wait for Spring?"],
                                                         "goals": ["Decide whether to list now or wait for spring"], "key_points": ["Fewer listings to compete with in winter"],
                                                         "takes": ["My take: decide on the price before you decide on the month."], "tone": "warm", "length": "standard", "cta": "home_value"})])

    from app.services import cma_claude
    from app.services.ai import ai_blog_writer_service
    monkeypatch.setattr(sug.ai_config_service, "get_config", config)
    monkeypatch.setattr(sug.ai_config_service, "estimate_cost_usd", lambda *_a: 0.00037)
    monkeypatch.setattr(sug, "reserve_copywrite", reserve)
    monkeypatch.setattr(sug, "record_usage_event", event)
    monkeypatch.setattr(sug, "reconcile_budget", reconcile)
    monkeypatch.setattr(cma_claude, "create", create)
    monkeypatch.setattr(ai_blog_writer_service, "_client", lambda: object())
    sug._CACHE.clear()
    user = SimpleNamespace(id="u1", plan_tier="IDX")
    title = "Should I sell my Wayne home before or after the holidays?"
    first = asyncio.run(sug.suggest(Db(), user, title=title, served_areas=["Villanova", "Wayne"]))
    assert first["ai"] and first["areas"] == ["Wayne"] and first["cost_usd"] == 0.00037 and first["takes"]
    again = asyncio.run(sug.suggest(Db(), user, title=title, served_areas=["Villanova", "Wayne"]))
    assert again.get("cached") and again["cost_usd"] == 0.0 and calls == {"ai": 1, "events": 1, "reconciled": [0.00037]}   # going back and forth never charges twice

    async def spent(*_a, **_k):
        raise sug.AIRateLimitExceeded("agent_monthly_budget", "limit reached")
    monkeypatch.setattr(sug, "reserve_copywrite", spent)
    blocked = asyncio.run(sug.suggest(Db(), user, title="Selling a home in Wayne this spring", served_areas=["Wayne"]))
    assert blocked["ai"] is False and blocked["areas"] == ["Wayne"] and blocked["key_points"] and calls["ai"] == 1          # built-in choices, no error, no call


# Owner, later 11/14: "are we using info like [their recent sales, their towns, their profile, their bio] to make the
# suggestions much more intelligent, obviously being based on the title first? And having title suggestions also be
# very intelligent?" The first build read the title and the town list only.
from datetime import date  # noqa: E402

TODAY = date(2026, 11, 14)
SALES = [
    {"city": "Wayne", "price": 450000, "sold_date": "2026-10-02"}, {"city": "Wayne", "price": 1200000, "sold_date": "2026-03-11"},
    {"city": "Wayne", "price": 780000, "sold_date": "2025-06-20"}, {"city": "Villanova", "price": 2400000, "sold_date": "2026-01-15"},
    {"city": "Radnor", "price": 900000, "sold_date": "2021-05-01"},                                       # too old to count
    {"city": "", "price": 500000, "sold_date": "2026-05-01"}, {"city": "Media", "price": None, "sold_date": "2026-08-09"},
]
CTX = {"month": "November", "month_no": 11, "year": 2026, "market": "Main Line", "towns": ["Villanova", "Wayne", "Bryn Mawr"],
       "about": "I help first-time buyers and downsizing sellers on the Main Line.",
       "sales": sug.sales_by_town(SALES, TODAY), "past_titles": ["Living in Wayne: a local guide", "Villanova market update"]}


def test_the_agents_sales_are_grouped_by_town_for_the_last_two_years():
    by = CTX["sales"]
    assert [(t["town"], t["count"]) for t in by] == [("Wayne", 3), ("Media", 1), ("Villanova", 1)]          # Radnor's 2021 sale is too old; a sale with no town is skipped
    assert (by[0]["low"], by[0]["high"], by[0]["newest"]) == (450000, 1200000, "2026-10-02") and by[1]["low"] is None


def test_the_model_is_told_who_the_agent_is_in_a_few_short_lines():
    text = sug.context_text(CTX, ["Wayne"], skip_title="Villanova market update")
    assert "Today: November 2026" in text and "Their market: Main Line" in text and "Towns on their website: Villanova, Wayne, Bryn Mawr" in text
    assert "by town: Wayne 3 ($450K to $1.2M); Villanova 1 ($2.4M); Media 1." in text      # the title's town, then their website's towns, then the rest
    assert "In Wayne, the title's town: they sold 3 homes in the last two years" in text
    assert "first-time buyers and downsizing sellers" in text
    assert '"Living in Wayne: a local guide"' in text and "Villanova market update" not in text.split("already wrote")[1]   # not the one being written
    assert len(text) < 900                                                                                    # a few hundred tokens at most
    none = sug.context_text({**CTX, "sales": []}, ["Ardmore"])
    assert "No closed sales of theirs on file" in none and "In Ardmore, the title's town: none of their sales on file" in none
    assert sug.context_text(None) == ""


def test_a_point_about_their_own_sales_is_offered_only_where_they_have_sales():
    wayne = "Should I sell my Wayne home before or after the holidays?"
    ai = {"key_points": ["Fewer listings to compete with in winter", "What a seller pays at settlement"],
          "takes": ["My take: price it right before you pick the month.", "My take: my recent sales prove winter works."],
          "angles": ["Sell Your Wayne Home Now or Wait for Spring?", "Living in Wayne: a local guide", "Wayne homes at $900,000: sell now?"]}
    out = sug.merge(sug.from_title(wayne, known_towns=CTX["towns"]), ai, wayne, context=CTX)
    assert out["sales_town"] == "Wayne" and out["key_points"][-1] == "What my recent Wayne sales show about pricing"   # always there when they sold there
    assert len(out["takes"]) == 2
    assert out["angles"] == ["Sell Your Wayne Home Now or Wait for Spring?"]            # never an article they already wrote, never a price
    ardmore = "Selling your Ardmore home this winter"
    ai2 = {"key_points": ["What my recent Ardmore sales show", "What a seller pays at settlement"], "takes": ["My take: I sold three homes here last winter."]}
    out2 = sug.merge(sug.from_title(ardmore, known_towns=["Ardmore"]), ai2, ardmore, context=CTX)
    assert out2["sales_town"] is None and out2["key_points"] == ["What a seller pays at settlement"] and out2["takes"] == []   # no Ardmore sales: no such claim


def test_the_title_says_which_price_range_of_their_sales_fits():
    assert sug.price_hint("Wayne homes under $600K: what you get") == {"band": None, "min": None, "max": 600000.0}
    assert sug.price_hint("Is $1.2 million the new normal in Villanova?")["max"] == 1500000.0
    assert sug.price_hint("Homes over $2M in Villanova")["min"] == 2000000.0
    assert sug.price_hint("Your first home in Ardmore: condo, townhome or twin?")["band"] == "low"
    assert sug.price_hint("Luxury estates in Villanova")["band"] == "high"
    assert sug.price_hint("What $500 a month in HOA fees buys you") == {"band": None, "min": None, "max": None}   # not a home price
    assert sug.from_title("Bryn Mawr market update", known_towns=["Bryn Mawr"])["price"] == {"band": None, "min": None, "max": None}


def test_title_ideas_come_from_the_agents_own_towns_sales_and_the_month():
    ideas = sug.code_titles(CTX)
    titles = [i["title"] for i in ideas]
    assert titles[0] == "What my recent Wayne sales say about pricing right now" and ideas[0]["basis"] == "your_sales"   # their strongest town first
    assert "Should you sell your Wayne home before or after the holidays?" in titles and "Villanova market update: November 2026" in titles
    assert "Buying your first home in Bryn Mawr: where to start" in titles and "Living in Media: a local guide" in titles
    assert sug.code_titles({"towns": [], "sales": [], "market": ""}) == []
    # an agent whose sales are million-dollar homes is not offered a "first home" title
    lux = {**CTX, "sales": sug.sales_by_town([{"city": "Villanova", "price": 2400000, "sold_date": "2026-01-15"}, {"city": "Wayne", "price": 1700000, "sold_date": "2026-02-01"}], TODAY)}
    assert not any("first home" in i["title"] for i in sug.code_titles(lux)) and any("what to check before you offer" in i["title"] for i in sug.code_titles(lux))
    # what the AI hands back is cleaned the same way
    got = sug.clean_titles([
        {"title": "Downsizing in Wayne: what to do before spring", "basis": "your_sales"},
        {"title": "What my recent Radnor sales say about pricing", "basis": "your_sales"},          # no Radnor sales in two years
        {"title": "Why Bryn Mawr is perfect for young families", "basis": "your_town"},              # fair housing
        {"title": "Villanova homes at $2,400,000: is it the peak?", "basis": "fresh"},               # a price
        {"title": "Living in Wayne: a local guide", "basis": "your_town"},                           # already written
        {"title": "Bryn Mawr in 2026: condo or twin?", "basis": "your_sales"},                       # no sales there -> "your town"
        {"title": "short", "basis": "fresh"}, "not a dict"], CTX)
    assert got == [{"title": "Downsizing in Wayne: what to do before spring", "basis": "your_sales"}, {"title": "Bryn Mawr in 2026: condo or twin?", "basis": "your_town"}]


def test_title_ideas_one_metered_call_a_day_and_plain_ideas_without_the_ai(monkeypatch):
    calls = {"ai": 0}

    async def config(_db):
        return {"copywrite_model": "claude-haiku-5-5"}

    async def reserve(*_a, **_k):
        return "reservation"

    async def event(*_a, **k):
        assert k["tool_names"] == ["blog_title_ideas"] and k["event_type"] == "blog_writer"

    async def reconcile(*_a):
        return None

    async def create(_client, **kw):
        calls["ai"] += 1
        msg = kw["messages"][0]["content"]
        assert "Wayne 3 ($450K to $1.2M)" in msg and "Starting topic they tapped: Sellers" in msg and kw["tools"][0]["name"] == "suggest_titles"
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=600, output_tokens=300), content=[SimpleNamespace(type="tool_use", name="suggest_titles", input={"titles": [
            {"title": "Selling your Wayne home this winter: where to start", "basis": "your_sales"},
            {"title": "Downsizing on the Main Line: what to do first", "basis": "fresh"},
            {"title": "Is winter a good time to sell in Villanova?", "basis": "timely"}]})])

    from app.services import cma_claude
    from app.services.ai import ai_blog_writer_service
    monkeypatch.setattr(sug.ai_config_service, "get_config", config)
    monkeypatch.setattr(sug.ai_config_service, "estimate_cost_usd", lambda *_a: 0.0002)
    monkeypatch.setattr(sug, "reserve_copywrite", reserve)
    monkeypatch.setattr(sug, "record_usage_event", event)
    monkeypatch.setattr(sug, "reconcile_budget", reconcile)
    monkeypatch.setattr(cma_claude, "create", create)
    monkeypatch.setattr(ai_blog_writer_service, "_client", lambda: object())
    sug._CACHE.clear()
    user = SimpleNamespace(id="u2", plan_tier="IDX")
    first = asyncio.run(sug.title_ideas(None, user, preset="Sellers", context=CTX))
    assert first["ai"] and len(first["titles"]) == 3 and first["cost_usd"] == 0.0002
    again = asyncio.run(sug.title_ideas(None, user, preset="Sellers", context=CTX))
    assert again.get("cached") and again["cost_usd"] == 0.0 and calls["ai"] == 1

    async def spent(*_a, **_k):
        raise sug.AIRateLimitExceeded("agent_monthly_budget", "limit reached")
    monkeypatch.setattr(sug, "reserve_copywrite", spent)
    plain = asyncio.run(sug.title_ideas(None, user, preset="Buyers", context=CTX))
    assert plain["ai"] is False and plain["titles"][0]["basis"] == "your_sales" and calls["ai"] == 1        # the allowance is spent: ideas still show, no call
    nothing = asyncio.run(sug.title_ideas(None, user, context={"towns": [], "sales": [], "market": "", "month": "November", "year": 2026}))
    assert nothing == {"titles": [], "ai": False, "cost_usd": 0.0}                                            # a brand-new account: no guesses
