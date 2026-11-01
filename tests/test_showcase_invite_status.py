"""Did the client get their Showcase invite (owner, 11/01)? services/showcase_invite_status."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import showcase_invite_status as s


def _row(status, link, at, template="showcase_ready", to="a@b.com", owner="o1", key="collection_link"):
    return SimpleNamespace(status=status, template_name=template, recipient_email=to, owner_id=owner,
                           template_variables={key: link}, created_at=at, sent_at=at if status == "SENT" else None,
                           error_message="Held back by the email rules" if status == "CANCELLED" else None)


class _DB:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _stmt):
        rows = self.rows
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


def _col(**kw):
    return {"id": "c1", "owner_id": "o1", "visitor_email": "A@b.com", "share_token": "tok123", "short_code": "abc",
            "notify_visitor": True, "status": "ACTIVE", **kw}


@pytest.mark.asyncio
async def test_sent_wins_over_a_later_hold_and_matches_by_short_code():
    now = datetime.now(timezone.utc)
    rows = [_row("CANCELLED", "https://x.com/showcase/abc", now), _row("SENT", "https://x.com/showcase/abc?x=1", now - timedelta(days=2))]
    st = (await s.invite_status(_DB(rows), [_col()]))["c1"]
    assert st["state"] == "sent" and st["can_send"]


@pytest.mark.asyncio
async def test_open_house_thank_you_counts_and_other_showcases_dont():
    now = datetime.now(timezone.utc)
    other = _row("SENT", "https://x.com/showcase/zzz", now)
    st = (await s.invite_status(_DB([other]), [_col()]))["c1"]
    assert st["state"] == "never"
    oh = _row("SENT", "https://x.com/showcase/tok123", now, template="visitor_confirmation", key="showcase_link")
    assert (await s.invite_status(_DB([oh]), [_col()]))["c1"]["state"] == "sent"


@pytest.mark.asyncio
async def test_held_pending_and_blocked():
    now = datetime.now(timezone.utc)
    held = (await s.invite_status(_DB([_row("CANCELLED", "https://x.com/showcase/abc", now)]), [_col()]))["c1"]
    assert held["state"] == "held" and held["why"] and held["can_send"]
    pending = (await s.invite_status(_DB([_row("PENDING", "https://x.com/showcase/abc", now)]), [_col()]))["c1"]
    assert pending["state"] == "sending" and not pending["can_send"]
    off = (await s.invite_status(_DB([]), [_col(notify_visitor=False)]))["c1"]
    assert off["blocked"] and not off["can_send"]
    no_email = (await s.invite_status(_DB([]), [_col(visitor_email=None)]))["c1"]
    assert no_email["blocked"] == "No email address"
