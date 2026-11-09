"""Write it for me (owner, 11/09): time words that stay right when a follow-up rolls over, and the AI's message
cleaned before it's saved (no links, no stray tokens, the open house date as a slot)."""
from datetime import date, datetime, timezone
from types import SimpleNamespace

from app.services import followup_writer as fw
from app.services import tasks_service as ts

SUN = date(2026, 11, 8)


def test_when_words_never_point_at_the_wrong_weekend():
    w = lambda n: ts.when_words(SUN, date.fromordinal(SUN.toordinal() + n))
    assert [w(0), w(1), w(2), w(6)] == ["today", "yesterday", "this past Sunday", "this past Sunday"]
    assert w(7) == "about a week ago" and w(9) == "about a week ago"
    assert w(12) == "a couple of weeks ago"            # NOT "last weekend" (that would be the 14th-15th)
    assert w(21) == "a few weeks ago" and w(40) == "about a month ago" and w(90) == "a couple of months ago" and w(200) == "a while back"


def test_fill_when_reads_right_each_day():
    msg = "Thanks again for coming to my open house at 239 Windham Dr {oh_when:2026-11-08}."
    day = lambda d: datetime(2026, 11, d, 15, tzinfo=timezone.utc)
    assert ts.fill_when(msg, day(9)).endswith("Windham Dr yesterday.")
    assert ts.fill_when(msg, day(10)).endswith("Windham Dr this past Sunday.")    # rolled over as overdue
    assert ts.fill_when(msg, day(25)).endswith("Windham Dr a couple of weeks ago.")
    assert ts.fill_when("no slot here") == "no slot here"
    assert ts.fill_when("bad {oh_when:2026-13-40}") == "bad recently"


def test_full_message_fills_the_slot():
    task = SimpleNamespace(suggested_message="Hi Nicole, it's Sam! Thanks for coming by {oh_when:2026-11-08}.", auto_source=None,
                           kind="text", completed_at=None, created_at=datetime.now(timezone.utc), notes=None)
    out = ts.full_message(task, None, None, lead_name="Nicole Williams")
    assert "{oh_when" not in out and "Thanks for coming by" in out


def test_clean_message():
    m = fw._clean_message("Hi Nicole! Thanks for coming {when}. See https://evil.example.com/x {first} ok", "2026-11-08")
    assert "{oh_when:2026-11-08}" in m and "http" not in m and "{first}" not in m and "  " not in m
    assert fw._clean_message("Thanks for coming {when}.", None) == "Thanks for coming recently."


def test_message_to_save_shape():
    # the saved form keeps the slot; the edit box shows today's words
    msg = "Hi! Thanks for coming {oh_when:2026-11-08}."
    assert "{oh_when" not in ts.fill_when(msg)
