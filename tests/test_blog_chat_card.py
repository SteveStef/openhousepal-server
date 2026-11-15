"""The chat helper's blog card is the Blogs page's writer, in a card (owner 11/14: "make sure the AI chat blog flow is
the same as our normal flow that you recently revamped ... and that the chat bot actually has that option as well").

Same title reading (same choices, same clean-up rules, same use of the agent's towns and sales), Save usage as the
default with Write it now beside it, and the chat never calls a slow Save usage article dead.
No network and no database."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.main  # noqa: F401
from app.services.ai import ai_actions_catalog as cat
from app.services.ai import ai_notify
from app.services.ai import blog_overnight_service as night
from app.services.ai import blog_suggest_service as bss

USER = SimpleNamespace(id="u1", plan_tier="IDX", state="PA")
CTX = {"month": "November", "month_no": 11, "year": 2026, "market": "Main Line", "about": "Careful valuation.", "towns": ["Wayne", "Villanova"],
       "sales": [{"town": "Villanova", "count": 3, "newest": "2026-09-01", "low": 900000, "high": 2400000, "mid": 1500000}], "past_titles": ["Living in Wayne: a local guide"]}
PAGE = {"audiences": ["Sellers"], "areas": ["Villanova"], "angles": ["Pricing a Villanova home this winter"], "goals": ["Know how to price before listing"],
        "key_points": ["How winter listings compare with spring", "What my recent Villanova sales show about pricing"], "takes": ["My take: price it right on day one"],
        "tone": "professional", "length": "standard", "cta": "home_value", "price": {"band": None, "min": 675000.0, "max": 1125000.0}, "sales_town": "Villanova", "ai": True}


def _patch(monkeypatch, suggestion, estimate=True):
    async def agent_context(_db, _user):
        return CTX

    async def suggest(_db, _user, *, title, context=None, **_k):
        return dict(suggestion)

    async def build_context(_db, _user):
        return {"served_areas": ["Wayne", "Villanova"], "area_cards": [{"name": "Villanova", "slug": "villanova-pa"}],
                "recent_sales": [{"id": 1, "address": "1 Main St", "city": "Villanova", "price": 950000, "sold_date": "2026-08-14", "image_url": None}],
                "listings": [], "videos": [{"id": f"v{i}", "title": f"Tour {i}", "url": f"https://youtu.be/abcdefg{i:02d}", "image": None, "kind": "tour"} for i in range(30)]}

    async def call_as_agent(_user, _method, path, params=None, **_k):
        if not estimate:
            raise cat.RouteError(503, "down")
        scale = {"short": 0.8, "standard": 1.0, "long": 1.3}[params["length"]]
        return {"usd": 0.97 * scale, "percent": round(4.9 * scale, 2),
                "overnight": {"usd": 0.52 * scale, "percent": round(2.6 * scale, 2), "typical_minutes": 20, "cap_minutes": 30, "max_minutes": 40}}
    from app.services.ai import ai_blog_writer_service
    monkeypatch.setattr(bss, "agent_context", agent_context)
    monkeypatch.setattr(bss, "suggest", suggest)
    monkeypatch.setattr(ai_blog_writer_service, "build_context", build_context)
    monkeypatch.setattr(cat, "call_as_agent", call_as_agent)


def test_the_card_starts_from_the_same_title_reading_as_the_page(monkeypatch):
    _patch(monkeypatch, PAGE)
    p = asyncio.run(cat._p_blog(None, USER, {"topic": "Pricing a $900K Villanova home this winter",
                                             "angles": ["Villanova sellers: winter pricing"], "goals": [], "point_ideas": ["Staging on a budget", "Homes under $500K sell fastest"]}))
    body, opts = p.payload["body"], p.payload["options"]
    # the reader, the town, the tone and the ending button come from the title - no typing, no asking in chat
    assert body["audiences"] == ["Sellers"] and body["areas"] == ["Villanova"] and body["area_slugs"] == ["villanova-pa"]
    assert body["tone"] == "professional" and body["cta"] == "home_value"
    # the page's choices first; the helper's own ideas behind them, through the same clean-up (no numbers in a key point)
    assert opts["angles"][0] == "Pricing a Villanova home this winter" and "Villanova sellers: winter pricing" in opts["angles"]
    assert opts["point_ideas"][:2] == PAGE["key_points"] and "Staging on a budget" in opts["point_ideas"]
    assert not any("$500K" in x for x in opts["point_ideas"])
    assert opts["takes"] == ["My take: price it right on day one"] and opts["sales_town"] == "Villanova" and opts["price"]["max"] == 1125000.0
    assert opts["sales"][0]["sold"] == "2026-08-14" and len(opts["videos"]) == 30        # every video, and the full sale date for "most recent"
    assert opts["said"] == {"tone": False, "length": False} and opts["paused"] is None


def test_what_the_agent_said_in_chat_wins_over_the_title(monkeypatch):
    _patch(monkeypatch, PAGE)
    p = asyncio.run(cat._p_blog(None, USER, {"topic": "Pricing a Villanova home", "tone": "luxury", "audiences": ["Luxury buyers"], "areas": ["Wayne"]}))
    body = p.payload["body"]
    assert body["tone"] == "luxury" and body["audiences"] == ["Luxury buyers"] and body["areas"] == ["Wayne"]
    assert p.payload["options"]["said"] == {"tone": True, "length": False}


def test_when_the_smart_reading_rests_the_helpers_ideas_are_cleaned_the_same_way(monkeypatch):
    resting = {**bss.merge(bss.from_title("Selling in Wayne this spring", known_towns=["Wayne", "Villanova"]), None, "Selling in Wayne this spring", context=CTX),
               "paused": {"reason": "nearly_out", "message": "This month's AI usage is almost used up."}}
    _patch(monkeypatch, resting)
    p = asyncio.run(cat._p_blog(None, USER, {"topic": "Selling in Wayne this spring", "angles": ["Wayne sellers: list now or wait"],
                                             "goals": ["Decide when to list"], "point_ideas": ["What my recent Wayne sales show", "Prep that matters before photos"]}))
    opts = p.payload["options"]
    assert opts["paused"] == "This month's AI usage is almost used up."
    assert "Wayne sellers: list now or wait" in opts["angles"] and opts["goals"] == ["Decide when to list"]
    assert "Prep that matters before photos" in opts["point_ideas"]
    assert not any("my recent wayne sales" in x.lower() for x in opts["point_ideas"])     # they have sold nothing in Wayne


def test_save_usage_is_the_default_and_write_it_now_is_one_tap(monkeypatch):
    _patch(monkeypatch, PAGE)
    p = asyncio.run(cat._p_blog(None, USER, {"topic": "Pricing a Villanova home"}))
    assert p.payload["body"]["overnight"] is True
    assert p.payload["percents"]["standard"] == 4.9 and p.payload["percents_save"]["standard"] == 2.6 and p.payload["percents_save"]["long"] == pytest.approx(3.38)
    assert p.payload["save_times"] == {"typical_minutes": 20, "max_minutes": 40, "cap_minutes": 30}
    now = cat._adjust_blog(USER, p.payload, {"when": "now"})
    assert now["body"]["overnight"] is False
    back = cat._adjust_blog(USER, now, {"when": "save"})
    assert back["body"]["overnight"] is True
    # they said "right now" in chat
    asked = asyncio.run(cat._p_blog(None, USER, {"topic": "Pricing a Villanova home", "speed": "now"}))
    assert asked.payload["body"]["overnight"] is False
    # no numbers for it (the estimate could not be read): never offered, never sent
    _patch(monkeypatch, PAGE, estimate=False)
    blind = asyncio.run(cat._p_blog(None, USER, {"topic": "Pricing a Villanova home"}))
    assert blind.payload["body"]["overnight"] is False and blind.payload["percents_save"] == {}
    assert cat._adjust_blog(USER, blind.payload, {"when": "save"})["body"]["overnight"] is False


def test_the_card_sends_the_choice_and_says_how_long(monkeypatch):
    sent = []

    async def run_route(_user, method, path, body=None, **_k):
        sent.append((method, path, body))
        return {"blog_id": "b1", "mode": "overnight", "max_minutes": 40} if (body or {}).get("overnight") else {"blog_id": "b1"}

    async def watch(*_a, **_k):
        return None
    monkeypatch.setattr(cat, "_run_route", run_route)
    monkeypatch.setattr(ai_notify, "watch", watch)
    out = asyncio.run(cat._r_blog(None, USER, {"body": {"topic": "T", "overnight": True}}))
    assert sent[0] == ("POST", "/studio/blogs/ai/generate-job", {"topic": "T", "overnight": True})
    assert "Save usage mode" in out["message"] and "about half the AI" in out["message"] and "40 at most" in out["message"] and out["working"] is True
    assert "overnight" not in out["message"].lower()
    fast = asyncio.run(cat._r_blog(None, USER, {"body": {"topic": "T", "overnight": False}}))
    assert "takes a few minutes" in fast["message"]


def test_the_chat_never_calls_a_slow_save_usage_article_dead():
    now = datetime.now(timezone.utc)
    quiet = (now - timedelta(minutes=25)).isoformat()
    normal = {"status": "running", "stage": "write", "stage_at": quiet, "started_at": quiet}
    assert ai_notify.blog_run_dead(normal, now)                                   # written now: 15 quiet minutes means it died
    state = night.new_state({"topic": "x"})
    slow = {**normal, "mode": "overnight", "overnight": state}
    assert not ai_notify.blog_run_dead(slow, now)                                 # Save usage: 25 quiet minutes is a slow line, not a dead run
    gone = {**slow, "overnight": {**state, "latest_at": (now - timedelta(hours=2)).isoformat()}}
    assert ai_notify.blog_run_dead(gone, now)                                     # past its own deadline
    assert ai_notify.blog_run_dead({"status": "failed"}, now) and not ai_notify.blog_run_dead({"status": "done"}, now)
