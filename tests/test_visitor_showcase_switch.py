"""The Visitors window's per-visitor Showcase switch beats the kit's Showcases setting both ways
(visitor_showcase_service) - and the sign-in path / Guest Pass read it the same way."""
from types import SimpleNamespace as NS

from app.services import visitor_showcase_service as vss


def _v(override=None, has_agent="NO"):
    return NS(showcase_override=override, has_agent=has_agent, email="a@example.com")


def test_switch_shows_kit_setting_until_the_agent_flips_it():
    assert vss.effective_on(NS(auto_showcase=True), _v()) is True
    assert vss.effective_on(NS(auto_showcase=False), _v()) is False
    assert vss.effective_on(NS(auto_showcase=False), _v(True)) is True
    assert vss.effective_on(NS(auto_showcase=True), _v(False)) is False


def test_guest_with_an_agent_never_gets_one():
    assert vss.effective_on(NS(auto_showcase=True), _v(None, "YES")) is False
    assert vss.effective_on(NS(auto_showcase=True), _v(True, "YES")) is False


def test_sign_in_build_follows_the_override():
    kit_off, kit_on = NS(auto_showcase=False), NS(auto_showcase=True)
    assert vss.wants_showcase(kit_off, _v()) is False
    assert vss.wants_showcase(kit_off, _v(), from_similar_qr=True) is True
    assert vss.wants_showcase(kit_off, _v(True)) is True
    assert vss.wants_showcase(kit_on, _v(False)) is False
    assert vss.wants_showcase(kit_on, _v(False), from_similar_qr=True) is False  # their own switch wins


def test_guest_pass_offer_respects_the_override(monkeypatch):
    from app.services import guest_pass_service as gp
    import app.utils.auth as auth
    monkeypatch.setattr(auth, "owner_has_access", lambda host, tiers: True)
    kit_off = NS(auto_showcase=False, pass_showcase=False)
    assert gp.showcases_offered(object(), _v(), kit_off) is False
    assert gp.showcases_offered(object(), _v(True), kit_off) is True
    assert gp.showcases_offered(object(), _v(False), NS(auto_showcase=True, pass_showcase=True)) is False


def test_follow_up_text_becomes_a_proper_email():
    from app.services import tasks_service as ts
    owner = NS(first_name="Quinn", last_name="Reviewer", brokerage="Main Line Test Realty")
    footer = ts.message_footer(owner)
    sms = ("Hi Jane, thanks for stopping by my open house at 12 Oak St! Your link is at the bottom of this text.\n\n"
           "Inside it, you can like or comment on any home - or just text me here.\n\n" + footer + "\n\nhttps://x.test/showcase/jane-1")
    body = ts.email_version(sms, owner, "(610) 555-0134")
    assert body.startswith("Hi Jane,\n\nThanks for stopping by")
    assert "bottom of this email" in body and "just reply to this email" in body
    assert "Your Showcase: https://x.test/showcase/jane-1" in body
    assert body.endswith("Best regards,\nQuinn Reviewer\nMain Line Test Realty\n(610) 555-0134")
    assert footer not in body
