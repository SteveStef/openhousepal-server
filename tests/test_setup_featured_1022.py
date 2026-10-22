"""10/22 setup-flow changes: Featured page tick boxes (old saves read the old way), the new open
house step + towns step for every plan, and the default email choices."""
import app.api.site_routes  # noqa: F401  (loads content_adapters in the app's import order)
from app.services.notification_preferences_service import default_prefs
from app.services.setup_flow_service import steps_for_plan
from app.services.website.content_adapters import _sort_cards, featured_sources


def test_old_saves_keep_their_meaning():
    assert featured_sources({"mode": "my_listings"}) == (True, False, False)
    assert featured_sources({"mode": "served_areas"}) == (True, True, False)
    assert featured_sources({"mode": "filtered", "prioritize_my_listings": True}) == (True, False, True)
    assert featured_sources({"mode": "filtered", "prioritize_my_listings": False}) == (False, False, True)
    assert featured_sources({}) == (True, False, False)


def test_tick_boxes_win_over_mode():
    s = {"mode": "my_listings", "show_mine": False, "show_area_homes": True, "show_custom": True}
    assert featured_sources(s) == (False, True, True)


def test_mixed_newest_orders_by_days_on_market():
    cards = [{"listing_key": "a", "days_on_market": 30}, {"listing_key": "b", "days_on_market": None}, {"listing_key": "c", "days_on_market": 2}]
    assert [c["listing_key"] for c in _sort_cards(cards, "newest", mixed=True)] == ["c", "a", "b"]
    assert [c["listing_key"] for c in _sort_cards(cards, "newest")] == ["a", "b", "c"]


def test_open_house_and_towns_steps_for_every_plan():
    for plan in ("NO_MLS", "BASIC", "PRO_NO_MLS", "PREMIUM", "IDX"):
        steps = steps_for_plan(plan, True)
        assert "openhouse" in steps and "areas" in steps
        assert steps.index("openhouse") < steps.index("alerts")
    assert "homes" not in steps_for_plan("PREMIUM", True)
    assert steps_for_plan(None, False) == ["about", "contact", "license", "photo", "story", "socials"]


def test_email_defaults_on_except_ai_media_and_listing_changes():
    d = default_prefs()
    off = {k for k, v in d.items() if not v["email"]}
    assert off == {"ai_media", "listing_updates"}
