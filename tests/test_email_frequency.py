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


def test_weekly_emails_stop_after_five_unopened():
    four = [unopened(7 * i + 2, WEEKLY_TEMPLATE) for i in range(WEEKLY_STRIKES - 1)]
    assert WEEKLY_STRIKES == 5
    assert rules(four)["mode"] == "weekly"
    assert rules(four + [unopened(60, WEEKLY_TEMPLATE)])["mode"] == "off"


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
