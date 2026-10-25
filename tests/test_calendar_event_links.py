"""Follow-up calendar events: every link is whole and clickable (user, 10/24 - in Google Calendar the
Showcase link "https://www.mainlinerealty.co/showcase/john-92w8" was only linked up to "showcase/",
"john-92w8" was plain text, so the link was dead). Offline: builds the event body, sends nothing."""
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.services import calendar_sync_service as cs  # noqa: E402
from app.services import tasks_service as ts  # noqa: E402

FULL = "https://www.mainlinerealty.co/showcase/john-92w8"
# Strict: the whole line is one URL made only of URL characters - nothing invisible, nothing glued on.
URL_LINE = re.compile(r"^https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+$")


def _task(**kw):
    base = dict(id="t1", owner_id="o1", lead_id="l1", title="Follow up with John", kind="call", priority="high",
                due_at=datetime.now(timezone.utc) + timedelta(days=1), all_day=False, completed_at=None,
                auto_source="open_house_signin", auto_source_id="v1", context="Signed in at your open house - 9 Ivy Ln",
                notes=None, suggested_message="Hi John, it was great meeting you at my open house! {showcase}".replace(
                    "{showcase}", ts.SHOWCASE_MARKER), created_at=datetime.now(timezone.utc), done_code="AbC-d_9x")
    base.update(kw)
    return SimpleNamespace(**base)


def _showcase(url=FULL):
    best = {"FullStreetAddress": "9 Ivy Ln", "City": "Wayne", "BedroomsTotal": 4, "BathroomsTotal": 2.5,
            "ListPrice": 850000}
    return {"url": url, "count": 6, "parts": ts._pitch_parts(6, best, 3, url), "first_name": "John"}


def _body(showcase, prop=None, task=None):
    code = "AbC-d_9x"
    return cs._task_event_body(task or _task(), showcase, "5", ts.done_url(code), owner=None,
                               contact_link=ts.contact_url(code), lead_name="John",
                               lead=SimpleNamespace(name="John Smith", phone="6105550188", email="john@example.com"),
                               prop=prop, showcase_link=ts.showcase_go_url(code))


def _url_lines(desc):
    return [l for l in desc.splitlines() if "http" in l]


def test_full_showcase_link_is_one_clean_line():
    desc = _body(_showcase())["description"]
    lines = desc.splitlines()
    # The client's own link, whole, on a line of its own (last line of the ready-to-send text).
    assert FULL in lines
    assert lines[-1] == FULL
    # Every line holding a link is exactly one URL - no label, emoji or invisible character glued on.
    for line in _url_lines(desc):
        assert URL_LINE.match(line), repr(line)


def test_agent_gets_a_short_showcase_link_near_the_top():
    desc = _body(_showcase())["description"]
    short = ts.showcase_go_url("AbC-d_9x")
    assert short.endswith("/d/AbC-d_9x/s")
    # Fits on one phone line (the done link's shape), so no calendar app has to wrap it.
    assert len(short.split("://", 1)[1]) <= 40 or "localhost" in short
    lines = desc.splitlines()
    assert short in lines
    assert lines.index(short) < lines.index(FULL)
    assert lines[lines.index(short) - 1] == "\U0001F449 Open John's Showcase:"


def test_home_inside_showcase_uses_the_short_link_too():
    prop = {"address": "9 Ivy Ln, Wayne, PA", "short": "9 Ivy Ln", "where": "showcase",
            "url": FULL + "/9-ivy-ln"}
    desc = _body(_showcase(), prop=prop)["description"]
    lines = desc.splitlines()
    short = ts.showcase_go_url("AbC-d_9x")
    assert lines.count(short) == 1          # one Showcase link up top, not two
    assert FULL + "/9-ivy-ln" not in desc   # the long one isn't the tap target any more


def test_invisible_characters_never_reach_a_link():
    dirty = "https://www.mainlinerealty.co/showcase/​john­-92w8﻿ "
    assert ts.clean_url(dirty) == FULL
    desc = _body(_showcase(dirty))["description"]
    assert FULL in desc.splitlines()
    for ch in ("​", "­", "﻿", "⁠", "\r", " "):
        assert ch not in desc


def test_agent_typed_notes_with_pasted_junk_are_cleaned():
    task = _task(notes="Your note: call after 5 pm\r\nsee​ https://example.com/a-b")
    desc = _body(_showcase(), task=task)["description"]
    assert "\r" not in desc and " " not in desc and "​" not in desc


def test_done_event_keeps_short_showcase_link_and_no_long_text():
    task = _task(completed_at=datetime.now(timezone.utc))
    desc = _body(_showcase(), task=task)["description"]
    assert ts.showcase_go_url("AbC-d_9x") in desc.splitlines()
    for line in _url_lines(desc):
        assert URL_LINE.match(line), repr(line)


def test_ics_feed_links_sit_on_their_own_lines():
    # The .ics feed folds long lines (RFC 5545); unfolded, each link must still be whole.
    line = ts._fold("DESCRIPTION:" + ts._ics_escape("Their Showcase:\n" + FULL))
    unfolded = line.replace("\r\n ", "")
    assert unfolded.endswith(ts._ics_escape(FULL))


# --------------------------------------------------------------------------- item 2: overdue roll-over
def _cand(street, dom, score=None):
    return {"home": {"FullStreetAddress": street, "City": "Wayne", "DaysOnMarket": dom}, "dom": dom, "score": score}


def test_rolling_overdue_keeps_the_same_home_and_counts_days():
    made = datetime.now(timezone.utc) - timedelta(days=2)
    # Made 2 days ago, when 9 Ivy Ln was 3 days on market (5 today) and the best match: still it.
    home, days = ts.pick_for_followup([_cand("9 Ivy Ln", 5, 95), _cand("12 Oak St", 15, 90)], made)
    assert home["FullStreetAddress"] == "9 Ivy Ln" and days == 5
    # A home that has aged past 3 weeks since is still named - it was fresh the day it was picked.
    home, days = ts.pick_for_followup([_cand("9 Ivy Ln", 23, 95)], datetime.now(timezone.utc) - timedelta(days=4))
    assert home["FullStreetAddress"] == "9 Ivy Ln" and days == 23


def test_a_newer_match_takes_over():
    made = datetime.now(timezone.utc) - timedelta(days=3)
    home, days = ts.pick_for_followup([_cand("9 Ivy Ln", 6, 99), _cand("4 Elm Rd", 1, 40)], made)
    assert home["FullStreetAddress"] == "4 Elm Rd" and days == 1


def test_message_and_text_show_todays_count():
    made = datetime.now(timezone.utc) - timedelta(days=1)
    task = _task(created_at=made)
    sc = _showcase()
    sc["candidates"] = [_cand("9 Ivy Ln", 4, 95)]
    msg = ts.full_message(task, sc, None)
    assert "9 Ivy Ln" in msg and "4 days ago" in msg
    sc["candidates"].append(_cand("4 Elm Rd", 0, 10))
    msg = ts.full_message(task, sc, None)
    assert "4 Elm Rd" in msg and "today" in msg


# --------------------------------------------------------------------------- item 3: Guest Pass activity
def test_guest_pass_activity_reaches_the_message_and_event():
    task = _task(notes="On their Guest Pass: opened 12 Oak St, Wayne\nOn their Guest Pass: opened the Cheat Sheet")
    msg = ts.full_message(task, _showcase(), None)
    assert "If 12 Oak St caught your eye too" in msg
    assert msg.index("12 Oak St caught") < msg.index("Showcase")
    desc = _body(_showcase(), task=task)["description"]
    assert "\U0001F440 ON THEIR GUEST PASS SINCE\n• Opened 12 Oak St, Wayne\n• Opened the Cheat Sheet" in desc
    assert "On their Guest Pass:" not in desc


# --------------------------------------------------------------------------- item 7: done -> undo
def test_done_then_undo_syncs_land_in_order(monkeypatch):
    """A slow "done" push can no longer land after a quick "undo" push for the same follow-up."""
    import asyncio
    log = []

    async def fake_locked(db, conn, task_id):
        log.append(("start", task_id, conn.tag))
        await asyncio.sleep(0.05 if conn.tag == "done" else 0)
        log.append(("end", task_id, conn.tag))

    monkeypatch.setattr(cs, "_sync_task_locked", fake_locked)

    async def run():
        done = SimpleNamespace(id="c1", tag="done")
        undo = SimpleNamespace(id="c1", tag="undo")
        await asyncio.gather(cs._sync_task(None, done, "t1"), cs._sync_task(None, undo, "t1"))

    asyncio.run(run())
    assert log == [("start", "t1", "done"), ("end", "t1", "done"), ("start", "t1", "undo"), ("end", "t1", "undo")]
    assert not cs._TASK_SYNC_LOCKS  # nothing left behind


def test_done_codes_are_letters_and_digits():
    for _ in range(200):
        code = ts.new_done_code()
        assert code.isalnum() and len(code) == 9


# --------------------------------------------------------------------------- item 4: "In 2 hrs"
def test_later_today_lands_about_two_hours_after():
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    came = datetime(2030, 6, 1, 13, 7, tzinfo=et)   # a Saturday open house, 1:07 PM
    start, end = ts.followup_window("later_today", came)
    assert start == datetime(2030, 6, 1, 15, 15, tzinfo=et) and end - start == timedelta(minutes=30)
    late = datetime(2030, 6, 1, 18, 40, tzinfo=et)  # +2h = 8:40 PM -> first thing next morning
    start, _ = ts.followup_window("later_today", late)
    assert start == datetime(2030, 6, 2, 8, 0, tzinfo=et)
    assert "later_today" in ts.WHEN_CHOICES
