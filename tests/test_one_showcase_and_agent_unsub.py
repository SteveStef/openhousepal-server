"""Owner, 11/16:
- ONE Showcase per agent per client: a second open house of the same agent keeps the Showcase (and
  link) they have - no second invite. Another agent's Showcase for the same person is never touched.
- Every unsubscribe link is per agent (whether the email went from the agent's own mailbox or from
  OpenHousePal): that agent's AUTOMATIC emails stop for good (the agent can't turn them back on), other
  agents' don't, and what the agent sends by hand still goes."""
import uuid

import pytest

NOW_TAG = "zqone"


async def _agents(db, tag, n=2):
    from app.models.database import User
    out = []
    for i in range(n):
        u = User(email=f"{tag}-a{i}@example.com", hashed_password="x", first_name=f"Agent{i}", last_name="Test", state="PA",
                 brokerage="Test Brokerage", mls_id=f"{tag.upper()}-{i}", plan_tier="IDX", subscription_status="ACTIVE")
        db.add(u)
        out.append(u)
    await db.commit()
    return out


def _showcase(owner, email, status="ACTIVE", reason=None):
    from app.models.database import Collection
    return Collection(owner_id=owner.id, name="1650 Skinners Turn Rd", visitor_email=email, visitor_name="Andrew Test",
                      share_token=uuid.uuid4().hex[:12], status=status, inactive_reason=reason)


async def _cleanup(tag):
    from sqlalchemy import delete, select
    from app.database import AsyncSessionLocal
    from app.models.database import ClientEmail, ClientEmailPref, Collection, Lead, LeadEvent, User
    async with AsyncSessionLocal() as db:
        ids = (await db.execute(select(User.id).where(User.email.like(f"{tag}%")))).scalars().all()
        if ids:
            await db.execute(delete(ClientEmail).where(ClientEmail.owner_id.in_(ids)))
            await db.execute(delete(ClientEmailPref).where(ClientEmailPref.owner_id.in_(ids)))
            await db.execute(delete(Collection).where(Collection.owner_id.in_(ids)))
            lids = (await db.execute(select(Lead.id).where(Lead.owner_id.in_(ids)))).scalars().all()
            if lids:
                await db.execute(delete(LeadEvent).where(LeadEvent.lead_id.in_(lids)))
            await db.execute(delete(Lead).where(Lead.owner_id.in_(ids)))
            await db.execute(delete(User).where(User.id.in_(ids)))
            await db.commit()


@pytest.mark.asyncio
async def test_one_showcase_per_agent_and_agents_stay_separate():
    import app.main  # noqa: F401
    from app.database import AsyncSessionLocal
    from app.services.open_house_service import OpenHouseService
    tag = f"{NOW_TAG}-{uuid.uuid4().hex[:8]}"
    guest = f"{tag}-andrew@example.com"
    try:
        async with AsyncSessionLocal() as db:
            jess, me = await _agents(db, tag)
            hers = _showcase(jess, guest)
            db.add(hers)
            await db.commit()
            # Jess's Showcase is never mine: I have none for him yet -> a new one gets built
            assert await OpenHouseService.reusable_showcase(db, me.id, guest) is None
            mine = _showcase(me, guest.upper())   # same person, typed in capitals
            db.add(mine)
            await db.commit()
            got = await OpenHouseService.reusable_showcase(db, me.id, guest)
            assert got is not None and got.id == mine.id
            assert (await OpenHouseService.reusable_showcase(db, jess.id, guest)).id == hers.id
            # asleep for no response -> a new sign-in wakes it and keeps it
            mine.status, mine.inactive_reason = "INACTIVE", "no_response"
            await db.commit()
            woke = await OpenHouseService.reusable_showcase(db, me.id, guest)
            assert woke is not None and woke.id == mine.id and woke.status == "ACTIVE"
            # switched off by the agent -> not reused (a fresh one, as before)
            mine.status, mine.inactive_reason = "INACTIVE", "agent_off"
            await db.commit()
            assert await OpenHouseService.reusable_showcase(db, me.id, guest) is None
    finally:
        await _cleanup(tag)


@pytest.mark.asyncio
async def test_reused_showcase_sends_no_second_invite(monkeypatch):
    from app.services.open_house_service import OpenHouseService

    async def reused(db, visitor, form_data):
        return {"success": True, "collection_id": "c1", "share_token": "tok", "reused": True}

    async def boom(*a, **k):
        raise AssertionError("a second 'your homes are ready' invite must not be queued")
    monkeypatch.setattr(OpenHouseService, "create_collection_for_visitor", staticmethod(reused))
    monkeypatch.setattr(OpenHouseService, "schedule_showcase_invite", staticmethod(boom))

    class F:
        open_house_event_id = "oh"
    out = await OpenHouseService.create_showcase_and_invite(None, object(), F())
    assert out["reused"] and out["share_token"] == "tok"


@pytest.mark.asyncio
async def test_unsubscribe_from_agents_own_mailbox_stops_only_that_agent():
    import app.main  # noqa: F401
    from app.database import AsyncSessionLocal
    from app.models.database import ClientEmail
    from app.services import client_email_service as ces
    from app.api.collections_routes import _agent_unsubscribe
    tag = f"{NOW_TAG}-{uuid.uuid4().hex[:8]}"
    guest = f"{tag}-andrew@example.com"
    try:
        async with AsyncSessionLocal() as db:
            jess, me = await _agents(db, tag)
            rec = ClientEmail(owner_id=me.id, recipient_email=guest, template="showcase_daily_digest", subject="x",
                              kind="auto", status="sent", via="mailbox")
            db.add(rec)
            await db.commit()
            # the wrong address for that email id -> not an agent-only unsubscribe
            assert await _agent_unsubscribe(db, f"other-{guest}", rec.id) is None
            out = await _agent_unsubscribe(db, guest.upper(), rec.id)
            assert out["scope"] == "agent" and "Agent1 Test" in out["message"]
            assert await ces.unsubscribed_from_agent(db, me.id, guest)
            assert not await ces.unsubscribed_from_agent(db, jess.id, guest)
            st = await ces.status_for(db, me.id, guest)
            assert st["mode"] == "off" and "Unsubscribed from your emails" in (st.get("reason") or "")
            from app.services.blacklist_service import BlacklistService
            assert await BlacklistService.is_blacklisted(db, guest, me.id)                    # my automatic emails: held
            assert not await BlacklistService.is_blacklisted(db, guest, me.id, manual=True)   # what I send by hand: goes
            assert not await BlacklistService.is_blacklisted(db, guest, jess.id)              # Jess: untouched
            assert not await BlacklistService.is_blacklisted(db, guest)                       # nobody else's list
            # the agent can't switch it back on, and deleting the lead doesn't wipe it
            st = await ces.set_manual(db, me.id, guest, True)
            assert st["mode"] == "off"
            await ces.stop_for_deleted_lead(db, me.id, guest)
            await ces.lift_lead_deleted(db, me.id, guest)
            await db.commit()
            assert await ces.unsubscribed_from_agent(db, me.id, guest)
            await ces.set_frequency(db, me.id, guest, "weekly", by="agent")
            assert await ces.unsubscribed_from_agent(db, me.id, guest)
        # my automatic emails stop; my hand-sent ones still go; Jess's emails still go
        assert await ces.before_send(me.id, guest, "showcase_daily_digest", "New homes", {}) is None
        assert await ces.before_send(me.id, guest, "visitor_confirmation", "Your homes", {}) is None
        assert await ces.before_send(me.id, guest, "visitor_confirmation", "Your homes", {"_manual": True}) is not None
        assert await ces.before_send(me.id, guest, "tour_confirmed", "Tour", {}) is not None
        assert await ces.before_send(jess.id, guest, "showcase_daily_digest", "New homes", {}) is not None
    finally:
        await _cleanup(tag)


def test_mailbox_unsubscribe_links_are_tagged():
    from app.services.email_service import SHOWCASE_UNSUBSCRIBE_TEMPLATES, unsubscribe_headers, with_unsub_tag
    assert with_unsub_tag("https://openhousepal.com/unsubscribe?email=a%40b.com", "abc") == "https://openhousepal.com/unsubscribe?email=a%40b.com&t=abc"
    assert with_unsub_tag("https://x/unsubscribe?email=a", None) == "https://x/unsubscribe?email=a"
    tpl = next(iter(SHOWCASE_UNSUBSCRIBE_TEMPLATES))
    h = unsubscribe_headers(tpl, "a@b.com", {}, track_id="abc")["List-Unsubscribe"]
    assert h.count("t=abc") == 2
    assert "t=" not in unsubscribe_headers(tpl, "a@b.com", {})["List-Unsubscribe"]



@pytest.mark.asyncio
async def test_every_unsubscribe_link_names_its_agent():
    """An email OpenHousePal sends for an agent (not only one from the agent's mailbox) carries a signed
    agent tag; that link unsubscribes from that agent only. A forged or mismatched tag does nothing."""
    import app.main  # noqa: F401
    from app.database import AsyncSessionLocal
    from app.services import client_email_service as ces
    from app.services.email_service import agent_from_unsub_token, agent_unsub_token, unsubscribe_headers, SHOWCASE_UNSUBSCRIBE_TEMPLATES
    from app.api.collections_routes import _agent_unsubscribe
    tag = f"{NOW_TAG}-{uuid.uuid4().hex[:8]}"
    guest = f"{tag}-andrew@example.com"
    try:
        async with AsyncSessionLocal() as db:
            jess, me = await _agents(db, tag)
        tok = agent_unsub_token(me.id, guest)
        assert agent_from_unsub_token(tok, guest.upper()) == me.id
        assert agent_from_unsub_token(tok, "someone-else@example.com") is None
        assert agent_from_unsub_token(jess.id + "." + tok.split(".")[1], guest) is None
        # the send pipeline tags the footer link of any email sent for an agent
        gate = await ces.before_send(jess.id, guest, "showcase_daily_digest", "New homes",
                                     {"Unsub": f"https://openhousepal.com/unsubscribe?email={guest}"})
        unsub = gate[1]["Unsub"]
        assert f"a={agent_unsub_token(jess.id, guest)}" in unsub
        tpl = next(iter(SHOWCASE_UNSUBSCRIBE_TEMPLATES))
        assert unsubscribe_headers(tpl, guest, {"Unsub": unsub})["List-Unsubscribe"].count("a=") == 2
        async with AsyncSessionLocal() as db:
            assert await _agent_unsubscribe(db, guest, None, "forged.abc") is None
            out = await _agent_unsubscribe(db, guest, None, agent_unsub_token(jess.id, guest))
            assert out["scope"] == "agent"
            assert await ces.unsubscribed_from_agent(db, jess.id, guest)
            assert not await ces.unsubscribed_from_agent(db, me.id, guest)
    finally:
        await _cleanup(tag)
