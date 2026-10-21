"""old_links_service: which old-site addresses are pages, and where each one goes on the new site."""
import pytest

from app.services.website.old_links_service import MatchContext, canonical_old_path, match_path, normalize_path


CTX = MatchContext(
    pages=[("about", "About"), ("contact", "Contact"), ("buyers-guide", "Buyer's Guide")],
    blogs=[("main-line-market-update-fall-2026", "Main Line Market Update: Fall 2026"),
           ("best-pizza-in-ardmore", "The Best Pizza in Ardmore"), ("buying-a-home-in-2027", "Buying a Home in 2027")],
    areas=[("villanova-pa", "Villanova"), ("bryn-mawr-pa", "Bryn Mawr"), ("wayne-pa", "Wayne"), ("ardmore-pa", "Ardmore"), ("gladwyne-pa", "Gladwyne")],
    listings=[("PAMC123456", "1450 Old Gulph Rd", "Villanova"), ("PADE999", "12 N Wayne Ave", "Wayne")],
    has_reviews=False, has_hub=True,
)


@pytest.mark.parametrize("raw,expected", [
    ("https://www.mainlinerealty.co/About/", "/about"),
    ("https://mainlinerealty.co/places/Bryn%20Mawr?ref=x#top", "/places/bryn mawr"),
    ("/blog//my-post/", "/blog/my-post"),
    ("/", "/"),
])
def test_normalize_path(raw, expected):
    assert normalize_path(raw) == expected


@pytest.mark.parametrize("raw", [
    "/", "/_next/static/chunks/main.js", "/wp-content/uploads/2020/01/house.jpg", "/wp-json/wp/v2/posts",
    "/feed", "/favicon.ico", "/brochure.pdf", "/sitemap.xml", "/cdn-cgi/l/email-protection", "/style.css",
    "/ohp-forward-check", "/wp-admin/admin-ajax.php",
])
def test_not_pages(raw):
    assert canonical_old_path(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("/blog/my-post/feed/", "/blog/my-post"),
    ("/blog/page/3", "/blog"),
    ("/my-post/amp", "/my-post"),
    ("/feedback", "/feedback"),          # not /feed
    ("/administration-team", "/administration-team"),  # not /admin
])
def test_canonical_strips_wrappers(raw, expected):
    assert canonical_old_path(raw) == expected


@pytest.mark.parametrize("path,target", [
    # the real old mainlinerealty.co site (Internet Archive, 10/20)
    ("/places/villanova", "/villanova-pa"),
    ("/places/bryn mawr", "/bryn-mawr-pa"),
    ("/media", "/blog"),
    # town pages by name
    ("/communities/wayne-pa", "/wayne-pa"),
    ("/bryn-mawr-homes-for-sale", "/bryn-mawr-pa"),
    ("/neighborhoods/ardmore", "/ardmore-pa"),
    # blog posts by slug / title
    ("/2024/10/12/main-line-market-update-fall-2026", "/blog/main-line-market-update-fall-2026"),
    ("/blog/the-best-pizza-in-ardmore", "/blog/best-pizza-in-ardmore"),
    ("/category/market-news", "/blog"),
    # the agent's own pages and built-in sections
    ("/about-us", "/about"),
    ("/meet-the-team", "/about"),
    ("/contact-me", "/contact"),
    ("/buyers-guide", "/buyers-guide"),
    ("/home-valuation", "/home-value"),
    ("/whats-my-home-worth", "/home-value"),
    ("/property-search", "/search"),
    ("/featured-listings", "/properties"),
    ("/sold-listings", "/sales"),
    ("/testimonials", "/about"),     # no Reviews page on -> About
    ("/index.html", "/"),
    # listings: their own home -> that listing; anything else -> its town / search
    ("/listings/1450-old-gulph-rd-villanova-pa-19085", "/properties/PAMC123456"),
    ("/property/12-n-wayne-ave", "/properties/PADE999"),
    ("/idx/details/77-lancaster-ave-wayne-pa", "/wayne-pa"),
    ("/homes-for-sale-details/88-maple-st-norristown-pa", "/search"),
    # from a real old Main Line agent site in the Archive (mainlinerealestate.com, 10/20)
    ("/833brynmawr/img004.htm", "/bryn-mawr-pa"),            # glued address + town
    ("/ardmorehomesforsale", "/ardmore-pa"),
    ("/around-the-main-line/gladwyn-homes-for-sale", "/gladwyne-pa"),  # misspelled town
    ("/search-main-line-real-estate", "/search"),
    ("/seller_services.htm", "/home-value"),
    ("/buying-a-home", "/search"),                            # not the "Buying a home in 2027" post
    ("/resource-library/garage-sale-tips", "/blog"),
    ("/around-the-main-line/narberth-homes-for-sale", "/neighborhoods"),  # a town they don't have
    ("/client-testimonials", "/about"),
])
def test_matches(path, target):
    m = match_path(path, CTX)
    assert m.target == target, (path, m)
    assert m.confidence >= 0.6


def test_unknown_page_is_left_for_the_agent():
    m = match_path("/xyz-qwerty", CTX)
    assert m.target is None and m.confidence == 0


def test_blog_phrase_with_path_word_is_not_an_address():
    # "5 ... path ..." isn't 5 Path Rd
    m = match_path("/5-steps-on-the-path-to-owning-a-home", CTX)
    assert not (m.target or "").startswith("/properties/")


def test_live_paths_are_known():
    live = CTX.live_paths()
    assert {"/about", "/villanova-pa", "/blog/best-pizza-in-ardmore", "/sales"} <= live
    assert "/testimonials" not in live
