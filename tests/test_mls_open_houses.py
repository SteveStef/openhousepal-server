"""Merge rules for Bright open house slots (app/services/mls_open_houses.py). Fake rows only -
nothing here talks to Bright or the database."""
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo
from types import SimpleNamespace

from app.services.mls_open_houses import (
    merge_open_house_rows,
    open_houses_payload,
    rebuild_open_house_slots,
    single_columns,
    stored_slots,
    upcoming_mls_open_houses,
)

TODAY = date(2026, 10, 1)


def _utc(day: str, hhmm: str) -> str:
    """Bright sends real UTC - turn an Eastern wall-clock time into what Bright would send."""
    local = datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=ZoneInfo("America/New_York"))
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(day: str, start: str = "13:00", end: str = "15:00", key=None, status="Active", listing="L1"):
    row = {
        "OpenHouseListingKey": listing,
        "OpenHouseDate": f"{day}T00:00:00Z",
        "OpenHouseStartTime": _utc(day, start),
        "OpenHouseEndTime": _utc(day, end),
        "MlsStatus": status,
    }
    if key:
        row["OpenHouseKey"] = key
    return row


def test_keeps_every_slot_in_a_batch():
    slots = merge_open_house_rows([], [_row("2026-10-04", key="A"), _row("2026-10-05", "12:00", "14:00", key="B")], TODAY)
    assert [(s["date"], s["start"], s["end"]) for s in slots] == [
        ("2026-10-04", "13:00:00", "15:00:00"),
        ("2026-10-05", "12:00:00", "14:00:00"),
    ]


def test_modified_past_open_house_does_not_overwrite_upcoming():
    existing = merge_open_house_rows([], [_row("2026-10-04", key="A")], TODAY)
    slots = merge_open_house_rows(existing, [_row("2026-09-20", key="OLD")], TODAY)
    assert [s["key"] for s in slots] == ["A"]


def test_status_never_removes_a_slot():
    # Product rule: once seen, an open house stays until its day passes - status is ignored.
    existing = merge_open_house_rows([], [_row("2026-10-04", key="A"), _row("2026-10-05", key="B")], TODAY)
    slots = merge_open_house_rows(existing, [_row("2026-10-05", key="B", status="Cancelled")], TODAY)
    assert [s["key"] for s in slots] == ["A", "B"]


def test_reschedule_by_key_moves_the_slot():
    existing = merge_open_house_rows([], [_row("2026-10-04", key="A")], TODAY)
    slots = merge_open_house_rows(existing, [_row("2026-10-11", "11:00", "13:00", key="A")], TODAY)
    assert [(s["date"], s["start"]) for s in slots] == [("2026-10-11", "11:00:00")]


def test_past_slots_drop_and_soonest_columns_advance():
    existing = merge_open_house_rows([], [_row("2026-10-04", key="A"), _row("2026-10-11", key="B")], TODAY)
    later = merge_open_house_rows(existing, [], date(2026, 10, 6))
    assert [s["key"] for s in later] == ["B"]
    cols = single_columns(later)
    assert cols["mls_open_house_date"] == date(2026, 10, 11)
    assert cols["mls_open_house_start_time"] == time(13, 0)
    assert single_columns([])["mls_open_house_date"] is None


def test_daily_pass_adds_missed_slots_keeps_stored_and_drops_past():
    stored = merge_open_house_rows([], [_row("2026-10-04", key="A")], TODAY)
    # Bright's full set no longer lists A but has a new C - A is kept, C is added.
    slots = rebuild_open_house_slots(stored, [_row("2026-10-12", key="C")], TODAY)
    assert [s["key"] for s in slots] == ["A", "C"]
    # Once A's day has passed, the same pass removes it.
    assert [s["key"] for s in rebuild_open_house_slots(slots, [], date(2026, 10, 5))] == ["C"]


def test_legacy_single_columns_seed_the_merge():
    existing = stored_slots(None, date(2026, 10, 4), time(13, 0), time(15, 0))
    slots = merge_open_house_rows(existing, [_row("2026-10-05", key="B")], TODAY)
    assert [s["date"] for s in slots] == ["2026-10-04", "2026-10-05"]


def test_upcoming_helper_reads_list_then_falls_back():
    prop = SimpleNamespace(
        mls_open_houses=[
            {"key": "B", "date": "2026-10-05", "start": "12:00:00", "end": "14:00:00"},
            {"key": "A", "date": "2026-10-04", "start": "13:00:00", "end": None},
            {"key": "OLD", "date": "2026-09-01", "start": None, "end": None},
        ],
        mls_open_house_date=date(2026, 10, 4), mls_open_house_start_time=time(13, 0), mls_open_house_end_time=None,
    )
    assert [s["date"] for s in upcoming_mls_open_houses(prop, TODAY)] == [date(2026, 10, 4), date(2026, 10, 5)]
    assert open_houses_payload(prop, TODAY)[1] == {"date": "2026-10-05", "start": "12:00", "end": "14:00"}

    legacy = SimpleNamespace(mls_open_houses=None, mls_open_house_date=date(2026, 10, 4),
                             mls_open_house_start_time=time(13, 0), mls_open_house_end_time=time(15, 0))
    assert upcoming_mls_open_houses(legacy, TODAY) == [{"date": date(2026, 10, 4), "start": time(13, 0), "end": time(15, 0)}]
    legacy.mls_open_house_date = date(2026, 9, 1)
    assert upcoming_mls_open_houses(legacy, TODAY) == []


def test_bright_utc_times_become_eastern_wall_clock():
    # 239 Windham Dr, Sun Oct 4 2026: Bright sends 15:00Z-17:30Z - the open is 11:00 AM-1:30 PM EDT.
    row = {"OpenHouseKey": 1, "OpenHouseListingKey": "L1", "OpenHouseDate": "2026-10-04",
           "OpenHouseStartTime": "2026-10-04T15:00:00Z", "OpenHouseEndTime": "2026-10-04T17:30:00Z"}
    slots = merge_open_house_rows([], [row], TODAY)
    assert (slots[0]["start"], slots[0]["end"]) == ("11:00:00", "13:30:00")
    # Winter (EST, UTC-5): 18:00Z is 1:00 PM.
    winter = {**row, "OpenHouseKey": 2, "OpenHouseDate": "2027-01-10", "OpenHouseStartTime": "2027-01-10T18:00:00Z", "OpenHouseEndTime": "2027-01-10T20:00:00Z"}
    slots = merge_open_house_rows([], [winter], TODAY)
    assert (slots[0]["start"], slots[0]["end"]) == ("13:00:00", "15:00:00")
