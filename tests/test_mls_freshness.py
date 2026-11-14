"""Grader audit 11/13 (findings 1, 2, 24): CMAs and articles were made on MLS data 25 days old and
nothing said so - a closed sale showed as "pending", price cuts were missing, "homes for sale as of
Nov 13" came from Oct 19 data. app.services.mls_freshness is asked before every CMA and AI article."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.services import mls_freshness as mf  # noqa: E402

NOW = datetime(2026, 11, 13, 20, 0, tzinfo=timezone.utc)


def test_minutes_behind_is_fine():
    info = mf.describe(NOW - timedelta(minutes=5), now=NOW, enforced=True)
    assert info["level"] == "ok" and info["message"] is None and info["fresh_wording"]


def test_more_than_a_day_behind_warns_the_agent():
    info = mf.describe(NOW - timedelta(hours=30), now=NOW, enforced=True)
    assert info["level"] == "warn" and info["message"].startswith("Review before sending:")
    assert "1 day ago" in info["message"] and info["fresh_wording"]


def test_more_than_three_days_behind_is_refused_where_the_sync_should_be_running():
    info = mf.describe(datetime(2026, 10, 20, 3, 13, tzinfo=timezone.utc), now=NOW, enforced=True)
    assert info["level"] == "blocked" and info["age_days"] == 24
    assert "Oct 20, 2026" in mf.blocked_message(info) and "24 days behind" in mf.blocked_message(info)


def test_a_copy_that_never_syncs_is_labeled_not_refused():
    info = mf.describe(datetime(2026, 10, 20, 3, 13, tzinfo=timezone.utc), now=NOW, enforced=False)
    assert info["level"] == "warn" and "Oct 20, 2026" in info["message"] and "may have sold" in info["message"]


def test_articles_lose_right_now_wording_after_a_week():
    assert mf.describe(NOW - timedelta(days=6), now=NOW, enforced=False)["fresh_wording"]
    assert not mf.describe(NOW - timedelta(days=8), now=NOW, enforced=False)["fresh_wording"]


def test_no_listings_at_all_never_blocks():
    assert mf.describe(None, now=NOW, enforced=True)["level"] == "ok"
