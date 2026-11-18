"""How often a client gets automatic emails (owner, 11/07): the rules step down on their own, and a pick
of weekly or every two weeks stays put."""
from datetime import datetime, timedelta, timezone

from app.services.client_email_service import WEEKLY_STRIKES, WEEKLY_TEMPLATE, decide

NOW = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)


def unopened(days_ago: int, template: str = "showcase_daily_digest") -> dict:
    return {"sent_at": NOW - timedelta(days=days_ago), "opened_at": None, "clicked_at": None, "template": template}


def rules(emails, **kw):
    return decide(emails, manual=kw.pop("manual", None), reset_at=kw.pop("reset_at", None), last_engaged=None, now=NOW, **kw)


def test_three_unopened_days_step_down_to_weekly():
    assert rules([unopened(2), unopened(3), unopened(4)])["mode"] == "weekly"


def test_daily_counts_only_from_the_change():
    emails = [unopened(5), unopened(6), unopened(7)]
    assert rules(emails, reset_at=NOW - timedelta(days=1))["mode"] == "daily"


def test_two_unopened_weekly_after_three_daily_stop_them():
    """Owner, 11/18: 3 unopened days -> weekly, then 2 unopened weekly ones -> off. At most 5 unopened."""
    daily = [unopened(20), unopened(19), unopened(18)]
    assert WEEKLY_STRIKES == 2
    one_weekly = daily + [unopened(11, WEEKLY_TEMPLATE)]
    d = rules(one_weekly)
    assert (d["mode"], d["weekly_unopened"]) == ("weekly", 1)
    assert "1 more goes unopened" in d["reason"]
    d = rules(one_weekly + [unopened(4, WEEKLY_TEMPLATE)])
    assert (d["mode"], d["code"], d["unopened"]) == ("off", "weekly_unopened", 5)


def test_the_old_seven_unopened_still_weekly_case_is_off_now():
    # one lead had 7 sent, 0 opened and was still on weekly (owner, 11/18)
    seven = [unopened(30), unopened(29), unopened(28)] + [unopened(21 - 7 * i, WEEKLY_TEMPLATE) for i in range(3)] + [unopened(1)]
    assert rules(seven)["mode"] == "off"


def test_several_emails_on_one_day_count_once():
    # an open house thank-you + the evening update on the same day are one unopened day
    same_day = [unopened(5, "visitor_confirmation"), unopened(5), unopened(4)]
    assert rules(same_day)["mode"] == "daily"
    assert rules(same_day + [unopened(3)])["mode"] == "weekly"


def test_an_open_house_invite_counts_as_one_weekly_email():
    daily = [unopened(20), unopened(19), unopened(18)]
    assert rules(daily + [unopened(12, "visitor_confirmation")])["mode"] == "weekly"
    assert rules(daily + [unopened(12, "visitor_confirmation"), unopened(5, WEEKLY_TEMPLATE)])["mode"] == "off"


def test_an_open_puts_them_back_on_daily():
    emails = [unopened(20), unopened(19), unopened(18), unopened(11, WEEKLY_TEMPLATE), unopened(4, WEEKLY_TEMPLATE)]
    d = decide(emails, manual=None, reset_at=None, last_engaged=NOW - timedelta(days=2), now=NOW)
    assert d["mode"] == "daily"


def test_a_chosen_weekly_stays_weekly_however_many_go_unopened():
    ignored = [unopened(7 * i + 2, WEEKLY_TEMPLATE) for i in range(10)]
    d = rules(ignored, frequency="weekly", frequency_by="agent")
    assert (d["mode"], d["code"]) == ("weekly", "chosen")
    assert d["reason"].startswith("You chose")


def test_a_client_pick_of_every_two_weeks_stays_and_says_who():
    d = rules([unopened(i + 2) for i in range(6)], frequency="biweekly", frequency_by="client")
    assert (d["mode"], d["code"]) == ("biweekly", "chosen")
    assert d["reason"].startswith("They chose")


def test_unsubscribe_and_agent_off_beat_a_pick():
    assert rules([], frequency="weekly", unsubscribed=True)["mode"] == "off"
    assert rules([], frequency="weekly", manual="off")["mode"] == "off"
