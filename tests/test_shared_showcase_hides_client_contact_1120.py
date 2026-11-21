"""Day 19 audit 11/20: a Showcase link is readable by anyone who has it (the homepage once showed a real one) - its public
answer never carries the client's email or phone; the page only needs to know whether it's an anonymous visitor's."""
import app.main  # noqa: F401
from app.services.collections_service import _masked_email


def test_masked_email_never_shows_the_address():
    assert _masked_email("donna.pratte@gmail.com") == "d***@gmail.com"
    assert _masked_email("x@y.com") == "x***@y.com"
    assert _masked_email("") == "anonymous@visitor.com"
    assert _masked_email("no-at-sign") == "anonymous@visitor.com"
