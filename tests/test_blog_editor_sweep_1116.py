"""11/16 - the full test of editing and publishing a blog article before launch (a real browser, computer and phone
size, a local test agent). Two things it found in the website's own pages, checked here by reading those pages:

  1. The editor and its Preview ended the article with the small contact card whenever the "End with my email
     signature" switch was on - even for an agent who has never saved a signature. The published page (which asks
     the server whether a signature card exists) showed that agent their closing section and its button. So the
     agent saw their closing button missing while editing. The editor now asks the same question.
  2. The Blogs list said "No articles yet - publish your first piece" when its load failed (the server was
     restarting), and also when a search or a tab simply matched nothing.

(The third - a scheduled article never went live - is tests/test_blog_scheduled_publish.py.)"""
from pathlib import Path

FRONT = Path(__file__).resolve().parents[2] / "frontend"
EDITOR = (FRONT / "app" / "studio" / "blogs" / "[id]" / "page.tsx").read_text(encoding="utf-8")
LIST = (FRONT / "app" / "studio" / "blogs" / "page.tsx").read_text(encoding="utf-8")
PUBLIC = (FRONT / "app" / "[slug]" / "blogs" / "[blogSlug]" / "page.tsx").read_text(encoding="utf-8")


def test_the_editor_ends_the_article_the_way_the_published_page_does():
    # the published page goes by the server's answer ...
    assert "signatureHubSlug={blog.signatureCard ? params.slug : null}" in PUBLIC
    # ... and the editor now asks the same thing before it shows, instead of going by the switch alone
    assert "/signature-card`)).ok" in EDITOR and "setHasSignatureCard(card)" in EDITOR
    line = next(l for l in EDITOR.splitlines() if l.strip().startswith("const signatureOn ="))
    assert "use_signature_card !== false" in line and "hasEmailSignatureAccess(" in line and "&& hasSignatureCard" in line
    # the answer is known before the editor is shown (no flip from one ending to the other in front of the agent)
    assert EDITOR.index("setHasSignatureCard(card)") < EDITOR.index("setLoading(false)", EDITOR.index("setHasSignatureCard(card)"))
    # an agent with no signature is not offered a switch that cannot change anything, and the publish screen says why
    assert "hasEmailSignatureAccess(user?.plan_tier) && hasSignatureCard" in EDITOR
    assert "You have not made an email signature yet" in EDITOR


def test_a_plan_with_no_mls_reads_nothing_from_the_listings():
    # owner, 11/16: "with the no MLS plan it's the same exact thing, it just can't use data from our database or the
    # Bright MLS at all". Every place the article run reads listing numbers is behind the plan check.
    import inspect
    import re
    from app.services.ai import ai_blog_pipeline_service as pipe
    src = inspect.getsource(pipe._run)
    assert "mls_ok = tier_has_mls(user.plan_tier)" in src and "mls=mls_ok" in src
    assert "for i, a in enumerate(areas if mls_ok else [])" in src                       # per-town market numbers
    assert "if mls_ok and areas and not regional and wants_payment_example(brief):" in src   # the payment example
    assert "if areas and mls_ok else None" in src[src.index("older = await older_homes_share("):][:200]   # the age-of-homes look-up
    # nothing else in the run asks the listings for a number without that check
    calls = [m.start() for m in re.finditer(r"await (?:entry_price_profile|older_homes_share)\(", src)]
    for at in calls:
        assert "mls_ok" in src[max(0, at - 400):at + 260], src[at:at + 80]


def test_the_blogs_list_says_what_really_happened_when_no_card_shows():
    assert "else setLoadFailed(true)" in LIST and "We couldn&apos;t load your articles" in LIST
    # "No articles yet" only when the load worked and there truly are none
    at = LIST.index(">No articles yet<")
    shown_when = LIST[LIST.rindex("{!loading", 0, at):at].splitlines()[0]
    assert "!loadFailed && blogs.length === 0" in shown_when
    # a search or a tab that matches nothing says so, and one tap shows everything again
    assert "No article matches that search" in LIST and "setSearch(''); setStatusTab('ALL')" in LIST
