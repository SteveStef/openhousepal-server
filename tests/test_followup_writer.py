"""Write it for me (owner, 11/09): time words that stay right when a follow-up rolls over, and the AI's message
cleaned before it's saved (no links, no stray tokens, the open house date as a slot)."""
from datetime import date, datetime, timezone
from types import SimpleNamespace

from app.services import followup_writer as fw
from app.services import tasks_service as ts

SUN = date(2026, 11, 8)


def test_when_words_never_point_at_the_wrong_weekend():
    w = lambda n: ts.when_words(SUN, date.fromordinal(SUN.toordinal() + n))
    assert [w(0), w(1)] == ["today", "yesterday"]
    # "last weekend" = the weekend right before this (Monday-Sunday) week - Tuesday 11/10 through Sunday 11/15
    assert [w(n) for n in range(2, 8)] == ["last weekend"] * 6
    assert w(8) == "about a week ago" and w(10) == "about a week ago"   # Mon 11/16 on: the weekend before last
    assert w(12) == "a couple of weeks ago"            # NOT "last weekend" (that would be the 14th-15th)
    assert w(21) == "a few weeks ago" and w(40) == "about a month ago" and w(90) == "a couple of months ago" and w(200) == "a while back"


def test_when_words_the_owners_cases():
    MON = date(2026, 11, 9)
    assert ts.when_words(date(2026, 11, 7), MON) == "last weekend"          # Nicole: Saturday open house, texted Monday
    assert ts.when_words(date(2026, 11, 8), MON) == "yesterday"
    FRI = date(2026, 11, 13)
    assert ts.when_words(date(2026, 11, 10), FRI) == "earlier this week"     # a Tuesday open house, read Friday
    assert ts.when_words(date(2026, 11, 8), FRI) == "last weekend"
    assert ts.when_words(date(2026, 11, 5), FRI) == "last week"              # a Thursday of last week
    assert ts.when_words(date(2026, 11, 1), FRI) == "a couple of weeks ago"  # the weekend before last
    SUNDAY = date(2026, 11, 15)
    assert ts.when_words(date(2026, 11, 14), SUNDAY) == "yesterday"
    assert ts.when_words(date(2026, 11, 7), SUNDAY) == "last weekend"        # 8 days, still the weekend before this week
    assert ts.when_words(date(2026, 11, 1), SUNDAY) == "a couple of weeks ago"


def test_fill_when_reads_right_each_day():
    msg = "Thanks again for coming to my open house at 239 Windham Dr {oh_when:2026-11-08}."
    day = lambda d: datetime(2026, 11, d, 15, tzinfo=timezone.utc)
    assert ts.fill_when(msg, day(9)).endswith("Windham Dr yesterday.")
    assert ts.fill_when(msg, day(10)).endswith("Windham Dr last weekend.")    # rolled over as overdue
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


# ---- the open house follow-up says WHEN they came (owner, 11/09: Nicole's text, sent days later, never said) ----
OH_MSG = ("Hi Nicole, thanks for stopping by my open house at 239 Windham Dr, Exton! If you have any questions about the "
          "home or would like to see it a second time, I'd be happy to set up a tour for you.\n\n" + ts.SHOWCASE_MARKER)


def test_with_when_slot_goes_after_the_open_house():
    out = ts.with_when_slot(OH_MSG, date(2026, 11, 7))
    assert out.startswith("Hi Nicole, thanks for stopping by my open house at 239 Windham Dr, Exton {oh_when:2026-11-07}! If you")
    assert ts.with_when_slot(out, date(2026, 11, 1)) == out                       # one slot, never two
    said = "Hi Nicole, thanks for coming to my open house at 239 Windham Dr yesterday!"
    assert ts.with_when_slot(said, date(2026, 11, 7)) == said                     # already says when
    assert ts.with_when_slot("Hi Nicole, how's the search going?", date(2026, 11, 7)) == "Hi Nicole, how's the search going?"
    # a period after an address abbreviation isn't the end of the sentence ...
    assert ts.with_when_slot("Thanks for stopping by my open house at 12 N. Main St, Media!", date(2026, 11, 7)) == \
        "Thanks for stopping by my open house at 12 N. Main St, Media {oh_when:2026-11-07}!"
    # ... unless it was
    assert ts.with_when_slot("Thanks for stopping by my open house at 239 Windham Dr. If you have any questions about the "
                             "home or would like to see it again, just text me.", date(2026, 11, 7)).startswith(
        "Thanks for stopping by my open house at 239 Windham Dr {oh_when:2026-11-07}. If you")
    # repeat visits
    assert "again at 239 Windham Dr {oh_when:2026-11-07}!" in ts.with_when_slot("Hi Nicole, great to see you again at 239 Windham Dr! That's 2", date(2026, 11, 7))


def _oh_task(msg, created):
    return SimpleNamespace(suggested_message=msg, auto_source="open_house_signin", kind="call", completed_at=None,
                           created_at=created, notes=None)


def test_older_followup_without_slot_gets_its_when():
    """Nicole's follow-up was made at her Saturday 11/07 sign-in, before messages carried the date: on Monday
    11/09 it reads "last weekend"."""
    made = datetime(2026, 11, 7, 18, 7, tzinfo=timezone.utc)      # 1:07 PM Eastern, Saturday
    from unittest.mock import patch
    real = ts.fill_when
    for now, words in [(datetime(2026, 11, 7, 22, tzinfo=timezone.utc), "today"),
                       (datetime(2026, 11, 8, 15, tzinfo=timezone.utc), "yesterday"),
                       (datetime(2026, 11, 9, 15, tzinfo=timezone.utc), "last weekend"),
                       (datetime(2026, 11, 16, 15, tzinfo=timezone.utc), "about a week ago"),
                       (datetime(2026, 11, 25, 15, tzinfo=timezone.utc), "a couple of weeks ago")]:
        with patch.object(ts, "fill_when", lambda t, n=None, _now=now: real(t, _now)):
            out = ts.full_message(_oh_task(OH_MSG, made), None, None, lead_name="Nicole Williams")
        assert f"open house at 239 Windham Dr, Exton {words}! If you" in out, (words, out)
    # other kinds of follow-up are left alone
    other = SimpleNamespace(suggested_message="Hi Nicole, thanks for asking to see 239 Windham Dr!", auto_source="showing_request",
                            kind="text", completed_at=None, created_at=made, notes=None)
    assert ts.full_message(other, None, None) == "Hi Nicole, thanks for asking to see 239 Windham Dr!"


def test_new_followup_message_carries_the_visit_date():
    msg = ts.render_rule_message(None, "open_house_signin", name="Nicole Williams", address="239 Windham Dr, Exton")
    assert ts.with_when_slot(msg, date(2026, 11, 7)).startswith(
        "Hi Nicole, thanks for stopping by my open house at 239 Windham Dr, Exton {oh_when:2026-11-07}!")


def test_ai_message_gets_the_slot_and_loses_similar():
    m = fw._clean_message("Hi Nicole, it's Peter! Thanks again for coming to my open house at 239 Windham Dr! I found homes "
                          "similar to the one you toured.", "2026-11-07", smart=True)
    assert "239 Windham Dr {oh_when:2026-11-07}!" in m and "similar" not in m and "homes that might interest you." in m
    keep = fw._clean_message("Thanks for coming {when}. Homes similar to the one you toured are in it.", "2026-11-07")
    assert keep.count("{oh_when:") == 1 and "similar to the one you toured" in keep     # no must-haves: unchanged


# ---- a real sign-in on the local dev database: the follow-up stores the visit date (throwaway zqwhen-* agent) ----
import uuid
from datetime import timedelta

import pytest


@pytest.mark.asyncio
async def test_signin_followup_stores_the_visit_date():
    import app.main  # noqa: F401
    from sqlalchemy import delete, select
    from app.database import AsyncSessionLocal
    from app.models.database import AgentTask, Lead, OpenHouseEvent, OpenHouseVisitor, User
    tag = f"zqwhen-{uuid.uuid4().hex[:8]}"
    signed = datetime.now(timezone.utc) - timedelta(days=9)       # a sign-in typed in today for an open house 9 days back
    try:
        async with AsyncSessionLocal() as db:
            agent = User(email=f"{tag}@example.com", hashed_password="x", first_name="Test", last_name="Agent", state="PA",
                         brokerage="Test Brokerage", mls_id=tag.upper(), plan_tier="BASIC", subscription_status="ACTIVE")
            db.add(agent)
            await db.commit()
            oh = OpenHouseEvent(agent_id=agent.id, qr_code=tag, address="239 Windham Dr, Exton, PA 19341",
                                scheduled_date=ts._eastern_day(signed))
            lead = Lead(owner_id=agent.id, name="Nicole Testcase", email=f"{tag}-guest@example.com", phone="6105550123",
                        represented="no", status="new", tags=[])
            db.add_all([oh, lead])
            await db.commit()
            visitor = OpenHouseVisitor(full_name="Nicole Testcase", email=lead.email, phone=lead.phone, has_agent="NO",
                                       open_house_event_id=oh.id, qr_code=tag, lead_id=lead.id,
                                       created_at=signed + timedelta(days=1))   # Clipboard sign-in typed in the day after
            db.add(visitor)
            await db.commit()
            task = await ts.create_open_house_followup(db, owner_id=agent.id, lead=lead, signin_id=visitor.id,
                                                       property_address="239 Windham Dr, Exton, PA 19341", occurred_at=datetime.now(timezone.utc),
                                                       commit=True)
            assert task is not None
            day = ts._eastern_day(signed).isoformat()                 # the open house's own date, not the typing day
            assert f"{{oh_when:{day}}}!" in task.suggested_message, task.suggested_message
            shown = ts.full_message(task, None, agent, lead_name=lead.name)
            assert "{oh_when" not in shown and ts.when_words(ts._eastern_day(signed), ts._eastern_day(datetime.now(timezone.utc))) in shown
    finally:
        async with AsyncSessionLocal() as db:
            ids = (await db.execute(select(User.id).where(User.email.like(f"{tag}%")))).scalars().all()
            if ids:
                await db.execute(delete(AgentTask).where(AgentTask.owner_id.in_(ids)))
                await db.execute(delete(OpenHouseVisitor).where(OpenHouseVisitor.qr_code == tag))
                await db.execute(delete(OpenHouseEvent).where(OpenHouseEvent.agent_id.in_(ids)))
                await db.execute(delete(Lead).where(Lead.owner_id.in_(ids)))
                await db.execute(delete(User).where(User.id.in_(ids)))
                await db.commit()
