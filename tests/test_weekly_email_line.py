"""The client's weekly Showcase email (owner, 11/13): "We now send these once a week. Want daily updates
again? Just open your Showcase." is only true when the rules slowed them down. A client who PICKED
weekly is told so (with a Change-how-often link); a weekly set by the agent says what it is."""
import json
from pathlib import Path

from app.services.email_top_line import fix_weekly_line, weekly_words

ARCHIVE = Path(__file__).resolve().parents[2] / "_archive" / "mailgun_templates_2026-10-22" / "showcase_weekly_digest_v2.json"
OLD = "We now send these once a week. Want daily updates again? Just open your Showcase."
LINK = "https://openhousepal.com/s/abc?emails=1"


def _live_like_html():
    if ARCHIVE.exists():
        d = json.loads(ARCHIVE.read_text(encoding="utf-8"))
        src = next(v for v in d["versions"] if v.get("active"))["template"]
        from pybars import Compiler
        return str(Compiler().compile(src)({"total_count": 12}))
    return f'<div style="x">{OLD}</div>'


def test_slowed_by_rules_keeps_the_design_words():
    html = _live_like_html()
    assert OLD in html
    assert fix_weekly_line(html, {"manage_link": LINK}) is None
    assert weekly_words({"weekly_why": "", "manage_link": LINK}) is None


def test_client_picked_weekly():
    out = fix_weekly_line(_live_like_html(), {"weekly_why": "client", "weekly_frequency": "weekly", "manage_link": LINK})
    assert out is not None and OLD not in out
    assert "Once a week, as you picked." in out and f'href="{LINK}"' in out and "Change how often" in out


def test_client_picked_every_two_weeks():
    out = fix_weekly_line(_live_like_html(), {"weekly_why": "client", "weekly_frequency": "biweekly", "manage_link": LINK})
    assert "Every two weeks, as you picked." in out


def test_agent_set_weekly():
    out = fix_weekly_line(_live_like_html(), {"weekly_why": "agent", "weekly_frequency": "weekly", "manage_link": LINK})
    assert "Once a week, with everything new in one email." in out and OLD not in out


def test_no_link_still_fixes_words():
    out = fix_weekly_line(_live_like_html(), {"weekly_why": "client", "weekly_frequency": "weekly"})
    assert "Once a week, as you picked." in out and "Change how often" not in out


def test_design_changed_sends_normally():
    assert fix_weekly_line("<p>Something else entirely</p>", {"weekly_why": "client", "manage_link": LINK}) is None
