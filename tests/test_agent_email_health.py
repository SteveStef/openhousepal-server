"""Agent email health (agent_email_health_service): the unopened counter, the 5 / 5 thresholds, the
critical-email allowlist and the tracking links. Pure - no database, never sends an email."""
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MAILGUN_DEV", "yes")

from app.services import agent_email_health_service as h  # noqa: E402
from app.services.email_service import with_open_pixel  # noqa: E402

NOW = datetime(2026, 10, 24, 18, 0, tzinfo=timezone.utc)
P = {"showcase_daily_digest_agent", "new_lead_captured"}


def mails(n, *, start_days_ago=20, step_hours=24, template="showcase_daily_digest_agent", opened=(), tracked=True):
    out = []
    for i in range(n):
        at = NOW - timedelta(days=start_days_ago) + timedelta(hours=step_hours * i)
        out.append({"sent_at": at, "opened_at": at + timedelta(hours=1) if i in opened else None, "clicked_at": None,
                    "template": template, "tracked": tracked})
    return out


def test_four_unopened_is_still_normal():
    assert h.decide(mails(4), proven=P, now=NOW)["mode"] == "normal"


def test_five_unopened_in_a_row_warns():
    d = h.decide(mails(5), proven=P, now=NOW)
    assert d["mode"] == "warned" and d["unopened"] == 5


def test_any_open_restarts_the_count():
    # 6 sent, the 3rd opened -> only the 3 after it count
    d = h.decide(mails(6, opened={2}), proven=P, now=NOW)
    assert d["mode"] == "normal" and d["unopened"] == 3


def test_grace_period_newest_email_not_counted_yet():
    ms = mails(5, start_days_ago=4)  # the 5th was sent "now" - inside the 24h grace
    assert h.decide(ms, proven=P, now=NOW)["mode"] == "normal"


def test_untracked_and_unproven_templates_never_count():
    assert h.decide(mails(8, tracked=False), proven=P, now=NOW)["mode"] == "normal"
    assert h.decide(mails(8, template="spotlight_weekly_report"), proven=P, now=NOW)["mode"] == "normal"
    # proven=None (tests / admin preview) counts everything tracked
    assert h.decide(mails(8, template="spotlight_weekly_report"), proven=None, now=NOW)["mode"] == "warned"


def test_weekly_needs_five_more_after_the_warning():
    ms = mails(14, start_days_ago=20)
    warned_at = ms[4]["sent_at"] + timedelta(minutes=5)
    # 9 unopened after the warning but only on the stored "warned" state: -> weekly
    assert h.decide(ms, mode="warned", warned_at=warned_at, proven=P, now=NOW)["mode"] == "weekly"
    # only 4 after the warning -> still warned
    assert h.decide(ms[:9], mode="warned", warned_at=warned_at, proven=P, now=NOW)["mode"] == "warned"


def test_first_evaluation_with_a_long_backlog_only_warns():
    # an agent with 12 old unopened emails the day this ships gets the warning first, never straight to weekly
    assert h.decide(mails(12), proven=P, now=NOW)["mode"] == "warned"


def test_open_after_warning_or_weekly_goes_back_to_normal():
    ms = mails(14)
    warned_at = ms[4]["sent_at"] + timedelta(minutes=5)
    weekly_at = ms[9]["sent_at"] + timedelta(minutes=5)
    ms[12]["opened_at"] = ms[12]["sent_at"] + timedelta(hours=2)
    assert h.decide(ms, mode="weekly", warned_at=warned_at, weekly_at=weekly_at, proven=P, now=NOW)["mode"] == "normal"
    # manual "turn daily back on" = reset_at
    ms2 = mails(14)
    assert h.decide(ms2, mode="weekly", warned_at=warned_at, weekly_at=weekly_at, reset_at=NOW - timedelta(hours=1),
                    proven=P, now=NOW)["mode"] == "normal"


def test_weekly_stays_weekly_without_an_open():
    ms = mails(14)
    warned_at = ms[4]["sent_at"] + timedelta(minutes=5)
    weekly_at = ms[9]["sent_at"] + timedelta(minutes=5)
    assert h.decide(ms[:10], mode="weekly", warned_at=warned_at, weekly_at=weekly_at, proven=P, now=NOW)["mode"] == "weekly"


# ---------------------------------------------------------------- the allowlist

def test_critical_client_activity_never_waits():
    for t in ("new_lead_captured", "form_submission_received", "tour_request", "similar_property_visitor_tour_request",
              "invite_rsvp_agent", "open_house_recap", "property_comment", "refine_search_request", "host_request_received",
              "payment_failed", "password_reset", "verify_code", "agent_weekly_summary", "agent_email_test"):
        assert h.is_critical(t), t


def test_routine_emails_can_wait_but_not_when_always():
    for t in ("showcase_daily_digest_agent", "liked_homes_roundup", "spotlight_weekly_report", "studio_media_ready"):
        assert not h.is_critical(t), t
        assert h.is_critical(t, always=True)


def test_unknown_template_is_treated_as_critical():
    assert h.is_critical("some_new_agent_email_nobody_classified")


def test_lists_dont_overlap():
    assert not (h.CRITICAL_TEMPLATES & h.DIGESTIBLE_TEMPLATES)


def test_every_agent_email_in_the_code_is_classified():
    """Each email_agent(..., "<template>", ...) in the app sits in exactly one list - a new agent email
    must be classified on purpose (unknown ones are sent at once, never held)."""
    app = Path(__file__).resolve().parents[1] / "app"
    found = set()
    for f in app.rglob("*.py"):
        src = f.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"email_agent\([^\n]*?,\s*(?:None|f?\"[A-Z_{}.a-z()]*\"|[a-z_]+),\s*\"([a-z_0-9]+)\"", src):
            found.add(m.group(1))
    assert found, "scan found nothing"
    missing = sorted(t for t in found if t not in h.CRITICAL_TEMPLATES | h.DIGESTIBLE_TEMPLATES)
    assert not missing, missing


# ---------------------------------------------------------------- tracking links + pixel

def test_with_tracking_wraps_only_app_links(monkeypatch):
    monkeypatch.setenv("FRONTEND_URL", "https://openhousepal.com")
    monkeypatch.setenv("API_URL", "https://api.openhousepal.com")
    v = h.with_tracking("abc", {"link": "https://openhousepal.com/leads?lead=1"})
    assert v["track_open"] == "https://api.openhousepal.com/e/a/abc.gif"
    assert v["link"].startswith("https://api.openhousepal.com/e/a/abc/go?to=%2Fleads%3Flead%3D1")
    assert v["_orig_link"] == "https://openhousepal.com/leads?lead=1"
    other = h.with_tracking("abc", {"link": "https://evil.example.com/x"})
    assert other["link"] == "https://evil.example.com/x"


def test_click_redirect_never_leaves_the_app(monkeypatch):
    monkeypatch.setenv("FRONTEND_URL", "https://openhousepal.com")
    assert h.safe_target("%2Fleads%3Flead%3D1") == "https://openhousepal.com/leads?lead=1"
    for bad in ("https://evil.com", "//evil.com", "/\\evil.com", "%2F%2Fevil.com", None, "", "javascript:alert(1)"):
        assert h.safe_target(bad) == "https://openhousepal.com/", bad


def test_open_pixel_goes_before_body_end_once():
    html = "<html><body><p>Hi</p></body></html>"
    out = with_open_pixel(html, "https://api.openhousepal.com/e/a/x.gif")
    assert out.count("e/a/x.gif") == 1 and out.index("e/a/x.gif") < out.index("</body>")
    assert with_open_pixel(out, "https://api.openhousepal.com/e/a/x.gif") == out
    assert with_open_pixel(html, None) == html


def test_summary_and_test_emails_render():
    items = [{"t": "showcase_daily_digest_agent", "s": "Your daily briefing", "l": "https://openhousepal.com/dash", "at": NOW.isoformat()},
             {"t": "liked_homes_roundup", "s": "2 likes", "l": None, "at": NOW.isoformat()}]
    subj, html, text = h.render(h.SUMMARY_TEMPLATE, "Your week", {"items": items, "app_url": "https://openhousepal.com", "link": "x"})
    assert "1 daily briefing" in html and "Turn daily emails back on" in html and "2 likes" in text
    subj, html, text = h.render(h.TEST_TEMPLATE, "Test", {"link": "https://x/y", "app_url": "https://openhousepal.com"})
    assert "Not spam" in html and "https://x/y" in text


# ---------------------------------------------------------------- the send gate

def test_send_gated_holds_or_sends(monkeypatch):
    sent = []
    from app.services import agent_email_service
    monkeypatch.setattr(agent_email_service, "send", lambda to, t, s, v, reply_to=None: sent.append((t, v)))

    async def hold(*a, **k):
        return True

    async def go(*a, **k):
        return False

    monkeypatch.setattr(h, "should_hold", hold)
    asyncio.run(h.send_gated("u", "a@example.com", "liked_homes_roundup", "s", {"link": "x", "_orig_link": "y"}, None, False))
    assert sent == []
    monkeypatch.setattr(h, "should_hold", go)
    asyncio.run(h.send_gated("u", "a@example.com", "new_lead_captured", "s", {"link": "x", "_orig_link": "y"}, None, False))
    assert sent and "_orig_link" not in sent[0][1]


# ---------------------------------------------------------------- one of each kind per person per event per day

from app.services import email_dedupe_service as dd  # noqa: E402

DAY1 = datetime(2026, 10, 24, 15, 0, tzinfo=timezone.utc)   # 11 AM Eastern
DAY1_LATE = datetime(2026, 10, 25, 3, 30, tzinfo=timezone.utc)  # 11:30 PM Eastern - same ET day
DAY2 = datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)


def test_same_event_same_day_is_one_key():
    v = {"link": "https://openhousepal.com/invites/AAA/guests", "day_label": "Sat Oct 25", "guests_coming": 3}
    a = dd.key_for("Agent@Example.com ", "invite_day_before_agent", "Tomorrow: 3 coming to Fall party", v, now=DAY1)
    b = dd.key_for("agent@example.com", "invite_day_before_agent", "Tomorrow: 4 coming to Fall party", {**v, "guests_coming": 4}, now=DAY1_LATE)
    assert a and a == b and "example" not in a   # hashed, never the address


def test_different_event_or_day_or_person_is_never_blocked():
    v = {"link": "https://openhousepal.com/invites/AAA/guests", "day_label": "Sat Oct 25"}
    base = dd.key_for("a@example.com", "invite_day_before_agent", "s", v, now=DAY1)
    assert base != dd.key_for("a@example.com", "invite_day_before_agent", "s", {**v, "link": "https://openhousepal.com/invites/BBB/guests"}, now=DAY1)
    assert base != dd.key_for("a@example.com", "invite_day_before_agent", "s", v, now=DAY2)
    assert base != dd.key_for("b@example.com", "invite_day_before_agent", "s", v, now=DAY1)
    t1 = dd.key_for("c@example.com", "tour_confirmed", "Your tour of 1 Main St is confirmed - Sat at 2 PM", {}, now=DAY1)
    t2 = dd.key_for("c@example.com", "tour_confirmed", "Your tour of 9 Oak Ave is confirmed - Sat at 2 PM", {}, now=DAY1)
    t3 = dd.key_for("c@example.com", "tour_confirmed", "Your tour of 1 Main St is confirmed - Sat at 4 PM", {}, now=DAY1)
    assert len({t1, t2, t3}) == 3   # another home or a new time = a new email


def test_security_account_and_one_off_emails_never_checked():
    for t in ("verify_code", "password_reset", "payment_failed", "subscription_cancelled", "trial_ending_soon",
              "new_lead_captured", "form_submission_received", "property_comment", "tour_request"):
        assert dd.key_for("a@example.com", t, "same subject", {"link": "x"}, now=DAY1) is None, t


def test_no_event_variables_means_no_check():
    assert dd.key_for("a@example.com", "showcase_ready", "s", {}, now=DAY1) is None
    assert dd.key_for("a@example.com", "showcase_daily_digest_agent", "s", {}, now=DAY1)  # the briefing: one a day


def test_process_memory_claim_and_forget():
    k = dd.key_for("z@example.com", "spotlight_documents", "Docs for 5 Elm", {}, now=DAY1)
    dd.forget(k)
    assert dd._seen_here(k) is False and dd._seen_here(k) is True
    dd.forget(k)
    assert dd._seen_here(k) is False
    dd.forget(k)
