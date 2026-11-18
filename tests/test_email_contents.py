"""What one client email had in it (owner, 11/18): the subject-line fallback, counts, the one-line
summary and the row's photo (new match first, then the price change). Pure - no database."""
from types import SimpleNamespace

from app.services import email_contents_service as ec
from app.services.client_email_service import about_from


def rec(subject, template="showcase_daily_digest"):
    return SimpleNamespace(subject=subject, template=template, about=None)


def test_a_price_drop_subject_becomes_the_home_and_its_new_price():
    homes = ec._clean(ec._from_subject(rec("25 Spring Valley Rd dropped to $1,090,000")), "showcase_daily_digest")
    assert homes == [{"street_address": "25 Spring Valley Rd", "new_price": "$1,090,000", "price": "$1,090,000", "kind": "price_drop"}]
    new, drops, total = ec._counts(rec("25 Spring Valley Rd dropped to $1,090,000"), homes)
    assert (new, drops, total) == (0, 1, 1)
    assert ec._summary(rec("x"), homes, new, drops, total) == "Price drop: 25 Spring Valley Rd to $1,090,000"


def test_counts_come_from_the_subject_when_only_some_homes_were_kept():
    r = rec("3 new matches and 1 price drop in your Showcase today")
    homes = [{"street_address": "1 A St", "kind": "new"}]
    assert ec._counts(r, homes) == (3, 1, 4)
    assert ec._summary(r, homes, 3, 1, 4) == "3 new matches · 1 price drop"


def test_weekly_and_new_listing_subjects():
    assert ec._from_subject(rec("Your week: 12 Oak St just hit the market for $950,000"))[0] == {"street_address": "12 Oak St", "kind": "new", "price": "$950,000"}
    assert ec._counts(rec("Your week: 6 new matches in your Showcase"), []) == (6, 0, 6)


def test_the_photo_is_a_new_match_before_a_price_change():
    homes = [{"street_address": "A", "kind": "price_drop", "image": "https://x/drop.jpg"},
             {"street_address": "B", "kind": "new", "image": "https://x/new.jpg"}]
    assert ec._photo(homes) == "https://x/new.jpg"
    assert ec._photo(homes[:1]) == "https://x/drop.jpg"
    assert ec._photo([{"street_address": "C", "kind": "new"}]) is None


def test_digest_homes_keep_new_vs_price_drop():
    homes = ec._clean(about_from({"properties": [
        {"street_address": "1 A St", "is_price_drop": False, "image": "https://x/a.jpg", "price": "$1"},
        {"street_address": "2 B ST", "is_price_drop": True, "old_price": "$9", "new_price": "$8"}]}), "showcase_daily_digest")
    assert [h["kind"] for h in homes] == ["new", "price_drop"]
    assert homes[1]["street_address"] == "2 B St"


def test_a_spotlight_price_alert_saves_what_changed():
    about = about_from({"property_address": "26 Old Gulph Rd", "hero_image": "https://x/h.jpg", "price_changed": True,
                        "price_down": True, "old_price": "$1,200,000", "new_price": "$1,150,000"})
    assert about == [{"street_address": "26 Old Gulph Rd", "image": "https://x/h.jpg", "kind": "price_drop",
                      "old_price": "$1,200,000", "new_price": "$1,150,000", "price": "$1,150,000"}]


def test_one_home_personal_emails():
    assert ec._from_subject(rec("Your tour of 9 Oak Ave is confirmed - Friday", "tour_confirmed"))[0]["street_address"] == "9 Oak Ave"
    r = rec("Lisa shared 18 homes you might like near 2451 MIDTOWN AVE", "visitor_confirmation")
    homes = ec._clean(ec._from_subject(r), r.template)
    n, d, total = ec._counts(r, homes)
    assert total == 18 and ec._summary(r, homes, n, d, total) == "Open house follow-up for 2451 Midtown Ave · 18 homes picked for them"
