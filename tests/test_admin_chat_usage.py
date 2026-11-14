"""Admin Insights -> AI chat (admin_chat_usage_service): sessions from message times, feature groups,
card outcomes and the per-agent / everyone totals. Pure - no database."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import admin_chat_usage_service as svc  # noqa: E402
from app.services.admin_chat_usage_service import Card, Msg, Turn, Usage  # noqa: E402

T0 = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)   # 10:00 AM Eastern
ACTIONS = {"create_design", "create_form", "start_cma", "create_showcase"}


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def chat(owner="a1", conv="c1", times=(), replies_after=0.2):
    """The agent asks at each time; the reply comes `replies_after` minutes later."""
    out = []
    for t in times:
        out.append(Msg(at(t), "user", conv, owner))
        out.append(Msg(at(t + replies_after), "assistant", conv, owner))
    return out


# ---------------------------------------------------------------- sessions

def test_one_session_is_first_to_last_message_plus_reading_minute():
    s = svc.split_sessions(chat(times=(0, 5, 10)))
    assert len(s) == 1
    assert s[0].messages == 3 and s[0].replies == 3
    # 0 -> 10.2 minutes, plus 1 minute to read the last reply
    assert s[0].seconds == int(10.2 * 60) + 60


def test_more_than_15_minutes_quiet_starts_a_new_session():
    s = svc.split_sessions(chat(times=(0, 14)) + chat(times=(40,)))
    assert [x.messages for x in s] == [2, 1]
    # exactly 15 minutes of quiet is still the same session
    assert len(svc.split_sessions(chat(times=(0, 15.2)))) == 1


def test_a_note_posted_on_its_own_is_not_a_session():
    msgs = chat(times=(0,)) + [Msg(at(120), "assistant", "c1", "a1")]   # "Your CMA is ready" 2 hours later
    s = svc.split_sessions(msgs)
    assert len(s) == 1 and s[0].messages == 1


def test_sessions_span_chats_and_ignore_input_order():
    msgs = chat(conv="c1", times=(0,)) + chat(conv="c2", times=(3,))
    s = svc.split_sessions(list(reversed(msgs)))
    assert len(s) == 1 and s[0].messages == 2


# ---------------------------------------------------------------- features

def test_tool_groups():
    g = svc.tool_group
    assert g("create_design") == "design" and g("make_ai_media") == "design"
    assert g("search_showcase_homes") == "homes" and g("search_properties") == "homes"
    assert g("resend_showcase_invite") == "showcase" and g("remove_showcase_homes") == "showcase"
    assert g("create_invite") == "forms" and g("send_invite_to_leads") == "forms" and g("edit_form") == "forms"
    assert g("find_homes_to_host") == "open_houses" and g("request_to_host_several") == "open_houses"
    assert g("start_cma") == "cma" and g("write_blog_post") == "blog"
    assert g("create_followup") == "followups" and g("get_priority_summary") == "followups"
    assert g("search_leads") == "leads" and g("update_branding") == "settings"
    assert g("look_up_agent") == "agents" and g("something_new") == "other"


def test_card_states_and_labels():
    assert svc.card_state("done") == "done" and svc.card_state("working") == "done"
    assert svc.card_state("pending") == "skipped" and svc.card_state("cancelled") == "skipped" and svc.card_state("replaced") == "skipped"
    assert svc.card_state("failed") == "failed" and svc.card_state("stale") == "failed"
    assert svc.action_label("create_design") == "Made a design"
    assert svc.action_label("mark_guest_request_done") == "Mark guest request done"


def test_summary_counts_cards_lookups_cost_and_time():
    u = Usage(
        msgs=chat(conv="c1", times=(0, 2)) + chat(conv="c2", times=(60,)),
        cards=[Card(at(0.2), "a1", "c1", "create_design", "done"), Card(at(2.2), "a1", "c1", "create_form", "pending"),
               Card(at(60.2), "a1", "c2", "start_cma", "working")],
        turns=[Turn(at(0.2), "a1", "c1", ["create_design", "show_link"], 0.002, False),
               Turn(at(2.2), "a1", "c1", ["search_showcase_homes", "search_showcase_homes", "create_form"], 0.003, False),
               Turn(at(60.2), "a1", "c2", ["start_cma", "search_leads", "offer_choices"], 0.001, False),
               Turn(at(61), "a1", None, [], 0.0, True)],
    )
    s = svc.summarize(u, ACTIONS)
    assert s["chats"] == 2 and s["messages"] == 3 and s["replies"] == 3
    assert s["sessions"] == 2
    assert s["seconds"] == (int(2.2 * 60) + 60) + (int(0.2 * 60) + 60)
    assert s["longest_session_seconds"] == int(2.2 * 60) + 60
    assert s["active_days"] == 1
    assert abs(s["cost_usd"] - 0.006) < 1e-9
    assert s["cards_made"] == 3 and s["things_done"] == 2
    # show_link / offer_choices are reply buttons and card tools are counted as cards, not lookups
    assert s["lookups"] == 3
    assert s["blocked"] == 1
    groups = {g["key"]: g for g in s["groups"]}
    assert groups["homes"]["lookups"] == 2 and groups["leads"]["lookups"] == 1
    assert groups["design"]["done"] == 1 and groups["forms"]["made"] == 1 and groups["forms"]["done"] == 0
    assert groups["cma"]["done"] == 1
    acts = {a["tool"]: a for a in s["actions"]}
    assert acts["create_form"]["skipped"] == 1 and acts["create_design"]["done"] == 1 and acts["start_cma"]["done"] == 1


def test_empty_usage_is_all_zero():
    s = svc.summarize(Usage(), ACTIONS)
    assert s["messages"] == 0 and s["sessions"] == 0 and s["seconds"] == 0 and s["avg_session_seconds"] is None
    assert s["groups"] == [] and s["actions"] == []


# ---------------------------------------------------------------- series, hours, chats list

def test_series_buckets_by_eastern_day_and_counts_agents():
    now = datetime(2026, 11, 12, 20, 0, tzinfo=timezone.utc)
    late = datetime(2026, 11, 11, 3, 30, tzinfo=timezone.utc)   # 10:30 PM Eastern on the 10th
    msgs = chat(owner="a1", times=(0,)) + chat(owner="a2", conv="c9", times=(1,)) + [Msg(late, "user", "c3", "a1")]
    rows = svc.series_of(msgs, 7, now)
    assert len(rows) == 7 and rows[-1]["d"] == "2026-11-12"
    by = {r["d"]: r for r in rows}
    assert by["2026-11-10"] == {"d": "2026-11-10", "messages": 3, "chats": 3, "agents": 2}
    assert sum(r["messages"] for r in rows) == 3
    assert svc.series_of(msgs, 365, now)[-1]["d"] == "2026-11-09"   # weeks start Monday


def test_hours_are_eastern_and_agent_messages_only():
    h = svc.hours_of(chat(times=(0, 61)))
    assert h[10] == 1 and h[11] == 1 and sum(h) == 2


def test_conversations_list_is_newest_first_without_text():
    u = Usage(msgs=chat(conv="old", times=(0,)) + chat(conv="new", times=(30,)) + [Msg(at(90), "assistant", "note", "a1")],
              cards=[Card(at(30.2), "a1", "new", "create_showcase", "done")],
              turns=[Turn(at(30.2), "a1", "new", ["create_showcase", "search_leads"], 0.01, False)])
    convs = {"old": {"created_at": at(0), "archived_at": at(5)}, "new": {"created_at": at(30), "archived_at": None}}
    rows = svc.conversations_of(u, convs, ACTIONS)
    assert [r["id"] for r in rows] == ["new", "old"]          # a chat with only a posted note is left out
    new = rows[0]
    assert new["cards"] == [{"label": "Made a Showcase", "group": "Showcases", "state": "done"}]
    assert new["looked_up"] == ["Leads"] and new["things_done"] == 1 and abs(new["cost_usd"] - 0.01) < 1e-9
    assert rows[1]["deleted_by_agent"] is True
    assert not any(k in new for k in ("title", "content", "text"))


def test_everyone_totals_keep_each_agents_sessions_apart():
    # Two agents chatting at the same time are two sessions, and their times add up.
    a, b = chat(owner="a1", times=(0, 5)), chat(owner="a2", conv="c2", times=(2,))
    per = [svc.summarize(Usage(msgs=x), ACTIONS) for x in (a, b)]
    together = svc.summarize(Usage(msgs=a + b), ACTIONS, sessions=per[0]["_sessions"] + per[1]["_sessions"])
    assert together["sessions"] == 2 and together["messages"] == 3
    assert together["seconds"] == per[0]["seconds"] + per[1]["seconds"]
