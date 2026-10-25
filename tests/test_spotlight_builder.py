"""Listing Spotlight builder (10/24): agent-added open house dates, room check marks, the
signature-card default."""
from datetime import date, time

from app.services.property_website_service import _clean_chapters
from app.services.spotlight_open_houses import manual_slots


def test_manual_slots_only_upcoming_manual_dates_sorted():
    draft = {"open_houses": [
        {"date": "2026-11-15", "start_time": "11:00", "end_time": "13:00", "manual": True},
        {"date": "2026-11-08", "start_time": "13:00", "end_time": "15:00", "manual": True},
        {"date": "2026-10-01", "start_time": "13:00", "manual": True},  # past
        {"date": "2026-11-09", "start_time": "13:00"},  # an old copied Manager row, not manual
        {"date": "not-a-date", "manual": True},
    ]}
    slots = manual_slots(draft, today=date(2026, 10, 24))
    assert [s["date"] for s in slots] == [date(2026, 11, 8), date(2026, 11, 15)]
    assert slots[0]["start"] == time(13, 0) and slots[0]["end"] == time(15, 0)


def test_manual_slots_empty_draft():
    assert manual_slots(None) == []
    assert manual_slots({"open_houses": None}) == []


def test_chapter_confirmed_is_kept():
    out = _clean_chapters([
        {"id": "k", "type": "kitchen", "label": "Kitchen", "photos": ["https://x/1.jpg"], "confirmed": True},
        {"id": "p", "type": "pool_spa", "label": "Pool", "photos": []},
    ])
    assert out[0]["confirmed"] is True
    assert out[1]["confirmed"] is False


# ---- "Follow this home" daily check: what a follower hasn't been told yet (pure) ----
from types import SimpleNamespace

import pytest

from app.services.property_website_service import MAX_DOCUMENTS, _check_file, _clean_files
from app.services.spotlight_alerts_service import changes_for


def _sub(price=1_000_000, status="active", ohs=()):
    return SimpleNamespace(last_price=price, last_state={"status": status, "open_houses": list(ohs)})


def test_nothing_changed_means_no_email():
    assert changes_for(_sub(), {"price": 1_000_000, "status": "active", "open_houses": []}) == {}


def test_price_cut_reported_with_old_and_new():
    news = changes_for(_sub(), {"price": 950_000, "status": "active", "open_houses": []})
    assert news == {"price": {"new": 950_000.0, "old": 1_000_000.0, "down": True}}


def test_new_open_house_only_once_and_only_while_for_sale():
    oh = {"id": "manual-1", "date": "2026-11-08"}
    assert changes_for(_sub(), {"price": 1_000_000, "status": "active", "open_houses": [oh]})["open_houses"] == [oh]
    assert changes_for(_sub(ohs=["manual-1"]), {"price": 1_000_000, "status": "active", "open_houses": [oh]}) == {}
    assert "open_houses" not in changes_for(_sub(), {"price": 1_000_000, "status": "pending", "open_houses": [oh]})


def test_under_contract_then_sold_each_reported_once():
    assert changes_for(_sub(), {"price": 1_000_000, "status": "pending", "open_houses": []}) == {"status": "pending"}
    assert changes_for(_sub(status="pending"), {"price": 1_000_000, "status": "pending", "open_houses": []}) == {}
    assert changes_for(_sub(status="pending"), {"price": 1_000_000, "status": "sold", "open_houses": []}) == {"status": "sold"}


# ---- upload limits (owner, 10/24) ----
def test_upload_checks_type_content_and_size():
    _check_file(b"%PDF-1.7 ...", "application/pdf", {"application/pdf"}, 100, "A PDF")
    with pytest.raises(ValueError, match="real PDF"):
        _check_file(b"MZ\x90\x00", "application/pdf", {"application/pdf"}, 100, "A PDF")
    with pytest.raises(ValueError, match="PDF, JPG, or PNG"):
        _check_file(b"MZ", "application/x-msdownload", {"application/pdf", "image/png"}, 100, "A PDF")
    with pytest.raises(ValueError, match="MB or smaller"):
        _check_file(b"%PDF-" + b"0" * 200, "application/pdf", {"application/pdf"}, 100, "A PDF")
    with pytest.raises(ValueError, match="empty"):
        _check_file(b"", "image/png", {"image/png"}, 100, "An image")


def test_documents_capped_and_cleaned():
    rows = [{"label": f"Doc {i}", "url": f"https://cdn.example/d{i}.pdf"} for i in range(MAX_DOCUMENTS + 3)]
    rows.insert(0, {"label": "bad", "url": "javascript:alert(1)"})
    out = _clean_files(rows, "url", MAX_DOCUMENTS)
    assert len(out) == MAX_DOCUMENTS and all(r["url"].startswith("https://") for r in out)
