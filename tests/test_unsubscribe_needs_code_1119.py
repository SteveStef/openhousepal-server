"""Owner, 11/19: an unsubscribe link needs its code (the signed agent tag a=... or the email's own id t=...).
Without one nobody can be unsubscribed by someone who just knows their address - except an address we were
already emailing before codes were on every link (its old emails must keep working): that stops the agents who
had it then, and only them. Gmail / Yahoo's own Unsubscribe button carries the same code."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from test_one_showcase_and_agent_unsub import _agents, _cleanup, _showcase  # same folder (tests is not a package)

NOW_TAG = "zqcode"


@pytest.mark.asyncio
async def test_a_link_without_its_code_stops_nothing(monkeypatch):
    import app.main  # noqa: F401
    import app.api.collections_routes as cr
    from app.database import AsyncSessionLocal
    from app.services import client_email_service as ces
    monkeypatch.setattr(cr, "UNSUB_CODES_SINCE", datetime.now(timezone.utc) - timedelta(days=1))  # codes required already
    tag = f"{NOW_TAG}-{uuid.uuid4().hex[:8]}"
    guest = f"{tag}-new@example.com"
    try:
        async with AsyncSessionLocal() as db:
            jess, me = await _agents(db, tag)
            db.add(_showcase(jess, guest)); await db.commit()   # a client from today - after codes
            assert await cr._do_unsubscribe(db, guest, None, None) is None
            assert await cr._do_unsubscribe(db, guest, None, "forged.abc") is None
            assert await cr._do_unsubscribe(db, guest, "not-an-email-id", None) is None
            assert not await ces.unsubscribed_from_agent(db, jess.id, guest)
            who = await cr.unsubscribe_who(email=guest, t=None, a=None, db=db)
            assert who["ok"] is False and "security code" in who["message"]
            with pytest.raises(cr.HTTPException) as e:
                await cr.unsubscribe_visitor(request=None, email=guest, t=None, a=None, db=db)
            assert e.value.status_code == 403
    finally:
        await _cleanup(tag)


@pytest.mark.asyncio
async def test_coded_link_stops_only_that_agent(monkeypatch):
    import app.main  # noqa: F401
    import app.api.collections_routes as cr
    from app.database import AsyncSessionLocal
    from app.services import client_email_service as ces
    from app.services.email_service import agent_unsub_token
    monkeypatch.setattr(cr, "UNSUB_CODES_SINCE", datetime.now(timezone.utc) - timedelta(days=1))
    tag = f"{NOW_TAG}-{uuid.uuid4().hex[:8]}"
    guest = f"{tag}-coded@example.com"
    try:
        async with AsyncSessionLocal() as db:
            jess, me = await _agents(db, tag)
            who = await cr.unsubscribe_who(email=guest.upper(), t=None, a=agent_unsub_token(jess.id, guest), db=db)
            assert who == {"ok": True, "scope": "agent", "agent_name": "Agent0 Test"}
            out = await cr.unsubscribe_visitor(request=None, email=guest, t=None, a=agent_unsub_token(jess.id, guest), db=db)
            assert out["scope"] == "agent" and out["agent_name"] == "Agent0 Test"
            assert await ces.unsubscribed_from_agent(db, jess.id, guest)
            assert not await ces.unsubscribed_from_agent(db, me.id, guest)
    finally:
        await _cleanup(tag)


@pytest.mark.asyncio
async def test_old_address_without_code_stops_the_agents_who_had_it(monkeypatch):
    import app.main  # noqa: F401
    import app.api.collections_routes as cr
    from app.database import AsyncSessionLocal
    from app.services import client_email_service as ces
    monkeypatch.setattr(cr, "UNSUB_CODES_SINCE", datetime.now(timezone.utc) + timedelta(days=1))  # before codes
    tag = f"{NOW_TAG}-{uuid.uuid4().hex[:8]}"
    guest = f"{tag}-old@example.com"
    try:
        async with AsyncSessionLocal() as db:
            jess, me, third = await _agents(db, tag, n=3)
            db.add(_showcase(jess, guest)); db.add(_showcase(me, guest.upper())); await db.commit()
            who = await cr.unsubscribe_who(email=guest, t=None, a=None, db=db)
            assert who["ok"] and sorted(who["agent_names"]) == ["Agent0 Test", "Agent1 Test"]
            out = await cr.unsubscribe_one_click(email=guest, t=None, a=None, db=db)
            assert out == {"success": True}
            assert await ces.unsubscribed_from_agent(db, jess.id, guest)
            assert await ces.unsubscribed_from_agent(db, me.id, guest)
            assert not await ces.unsubscribed_from_agent(db, third.id, guest)   # never had them
    finally:
        await _cleanup(tag)


def test_gmail_button_carries_the_agent_code():
    from app.services.email_service import _unsub_params
    tags = _unsub_params({"Unsub": "https://openhousepal.com/unsubscribe?email=a%40b.com&a=agent1.abc123&t=e1"})
    assert tags == "&a=agent1.abc123&t=e1"
