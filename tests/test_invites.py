"""Invites (Forms & Invites -> Invites): presets, RSVP rules, leads, emails, plan access.

Runs against the local dev Postgres with throwaway agents (zqinv-*@example.com), all removed
afterward. No email is ever sent - EmailService.send_simple_message is replaced by a recorder and
every guest address is @example.com."""
import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.api import invites_routes as routes
from app.api.lead_routes import get_lead
from app.database import AsyncSessionLocal
from app.models.database import EventInvite, EventInviteRsvp, Lead, LeadEvent, Notification, User
from app.schemas.invites import InviteSave, InviteSendRequest, InviteStatusUpdate, RsvpSubmit
from app.schemas.leads import LeadDetailOut
from app.services import invites_service as svc
from app.services.personal_email_service import render as render_personal

PREFIX = "zqinv"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def _email(label: str) -> str:
    return f"{PREFIX}-{label}-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture(autouse=True)
def no_send(monkeypatch):
    """Record every email instead of sending, and skip the per-IP rate limit."""
    sent = []

    def fake_send(self, to_email, subject, template, template_variables, reply_to=None, from_name=None):
        sent.append({"to": to_email, "subject": subject, "template": template, "vars": dict(template_variables or {})})
        return 200, "recorded"

    from app.services.email_service import EmailService
    monkeypatch.setattr(EmailService, "send_simple_message", fake_send)
    import app.services.public_rate_limit as prl
    monkeypatch.setattr(prl, "check_public_submit", lambda request: None)
    return sent


@pytest_asyncio.fixture(autouse=True)
async def cleanup():
    yield
    async with AsyncSessionLocal() as db:
        users = (await db.execute(select(User.id).where(User.email.like(f"{PREFIX}-%")))).scalars().all()
        if users:
            await db.execute(delete(EventInvite).where(EventInvite.owner_id.in_(users)))
            await db.execute(delete(Notification).where(Notification.agent_id.in_(users)))
            await db.execute(delete(Lead).where(Lead.owner_id.in_(users)))
            await db.execute(delete(User).where(User.id.in_(users)))
        await db.commit()


async def _agent(db, label: str, **kw) -> User:
    u = User(email=_email(label), hashed_password="not-a-real-hash", first_name="Test", last_name="Agent", state="PA",
             brokerage="Test Brokerage", mls_id=f"ZQINV-{uuid.uuid4().hex[:8]}", plan_tier=kw.get("tier", "PREMIUM"),
             subscription_status=kw.get("status", "ACTIVE"))
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


def _save(kind: str, **kw) -> InviteSave:
    p = svc.PRESETS[kind]
    return InviteSave(invite_type=kind, title=kw.get("title", p["title"]), message=p["message"],
                      event_date=kw.get("event_date", date.today() + timedelta(days=10)), start_time="18:00", end_time="21:00",
                      place_name="The Test House", address="1 Test Ln, Wayne, PA", design=p["design"],
                      questions=p["questions"])


async def _settle():
    await asyncio.sleep(0.6)   # fire-and-forget email tasks


# ------------------------------------------------------------------ pure


def test_presets_are_complete():
    ids = {p["id"] for p in svc.presets_out()}
    assert ids == {"open_house", "brokers_open", "client_event", "custom"}
    assert svc.PRESETS["brokers_open"]["audience"] == "agents"
    for p in svc.presets_out():
        cleaned = svc.clean_questions(p["questions"])
        assert len(cleaned) == len(p["questions"]), p["id"]
        assert any(q.get("role") == "guests" for q in cleaned), p["id"]


def test_clean_questions_drops_bad_and_dedupes():
    out = svc.clean_questions([
        {"id": "a", "type": "short_text", "label": "Name?"},
        {"id": "a", "type": "short_text", "label": "Again"},
        {"id": "b", "type": "select", "label": "No options", "options": []},
        {"id": "c", "type": "bogus", "label": "x"},
        {"id": "d", "type": "multi_select", "label": "Pick", "options": [" A ", "", "B"], "role": "nope"},
    ])
    assert [q["id"] for q in out][:1] == ["a"] and len(out) == 3
    assert out[1]["id"] != "a"
    assert out[2]["options"] == ["A", "B"] and "role" not in out[2]


def test_guest_count_and_represented():
    qs = svc.clean_questions(svc.PRESETS["open_house"]["questions"])
    assert svc.guest_count_of(qs, {"party": "Just me"}, "yes") == 1
    assert svc.guest_count_of(qs, {"party": "5+"}, "yes") == 5
    assert svc.guest_count_of(qs, {"party": "3"}, "no") == 0
    assert svc.represented_of(qs, {"agent": "No"}) == "no"
    assert svc.represented_of(qs, {}) is None


def test_ics_and_when_label():
    inv = EventInvite(id="x", code="abc", title="Dinner, with friends", event_date=date(2026, 11, 7), start_time="18:00", end_time="21:30",
                      place_name="Vetri", address="1312 Spruce St")
    assert svc.when_label(inv) == "Saturday, November 7 · 6 - 9:30 PM"
    text = svc.ics_text(inv, agent_name="Quinn", url="https://openhousepal.com/i/abc")
    assert "DTSTART:20261107T230000Z" in text and "SUMMARY:Dinner\\, with friends" in text and text.endswith("\r\n")


def test_personal_templates_render():
    v = {"first_name": "Jo", "agent_name": "Quinn Reviewer", "agent_phone": "(610) 555-0101", "invite_title": "Broker's Open",
         "invite_when": "Tuesday, November 3 · 11 AM - 1 PM", "invite_place": "22 Birch Ln", "invite_link": "https://openhousepal.com/i/abc",
         "Unsub": "https://openhousepal.com/unsubscribe?email=jo%40example.com"}
    subject, html, text = render_personal("event_invite", "Broker's Open", v, link="")
    assert subject == "Broker's Open" and "https://openhousepal.com/i/abc" in html and "Unsubscribe" in html
    subject, html, text = render_personal("event_rsvp", "You're on the list", {**v, "rsvp_response": "yes", "guest_count": 2}, link="")
    assert "on the list for 2" in text and "https://openhousepal.com/i/abc" in text


# ------------------------------------------------------------------ end to end (local db)


@pytest.mark.asyncio
async def test_client_invite_rsvp_makes_a_lead_and_emails(no_send):
    async with AsyncSessionLocal() as db:
        agent = await _agent(db, "agent")
        created = await routes.create_invite(_save("client_event"), db=db, current_user=agent)
        assert created["code"] and created["share_url"].endswith(f"/i/{created['code']}")
        assert created["accent_color"].startswith("#")

        pub = await routes.public_invite(created["code"], host=None, db=db)
        assert pub["title"] == svc.PRESETS["client_event"]["title"] and pub["host_ok"] is True and "counts" not in pub

        guest = _email("jane")
        r1 = await routes.public_rsvp(created["code"], RsvpSubmit(
            response="yes", name="Jane Guest", email=guest,
            answers=[{"questionId": "party", "value": "3"}, {"questionId": "diet", "value": ["Vegan", "Nope"]}]), request=None, db=db)
        assert r1["guest_count"] == 3 and r1["token"] and r1["updated"] is False
        await _settle()

        rs = (await db.execute(select(EventInviteRsvp).where(EventInviteRsvp.invite_id == created["id"]))).scalars().all()
        assert len(rs) == 1 and rs[0].lead_id
        diet = [a for a in rs[0].answers if a["questionId"] == "diet"][0]
        assert diet["value"] == ["Vegan"]
        events = (await db.execute(select(LeadEvent).where(LeadEvent.lead_id == rs[0].lead_id))).scalars().all()
        assert [e.source_type for e in events] == ["invite_rsvp"]
        bell = (await db.execute(select(Notification).where(Notification.agent_id == agent.id))).scalars().all()
        assert bell and bell[0].type == "INVITE_RSVP" and "invite=" in bell[0].link

        # the lead page opens (no 500 on the new source type) and links to the RSVP
        lead_id = rs[0].lead_id
        db.expire_all()
        await db.refresh(agent)
        detail = await get_lead(lead_id, db=db, current_user=agent)
        detail = detail if isinstance(detail, LeadDetailOut) else LeadDetailOut.model_validate(detail)
        assert any(ev.source_type == "invite_rsvp" for ev in detail.events)

        templates = {m["template"] for m in no_send}
        assert {"new_lead_captured", "event_rsvp"} <= templates
        assert all(m["to"].endswith("@example.com") for m in no_send)

        # changing the answer with their token updates the same RSVP and adds one lead event
        r2 = await routes.public_rsvp(created["code"], RsvpSubmit(response="maybe", name="Jane Guest", email=guest, token=r1["token"],
                                                                  answers=[{"questionId": "party", "value": "2"}]), request=None, db=db)
        assert r2["updated"] is True and r2["guest_count"] == 2
        db.expire_all()
        await db.refresh(agent)
        assert len((await db.execute(select(EventInviteRsvp).where(EventInviteRsvp.invite_id == created["id"]))).scalars().all()) == 1
        assert len((await db.execute(select(LeadEvent).where(LeadEvent.lead_id == lead_id))).scalars().all()) == 2

        mine = await routes.public_my_rsvp(created["code"], r1["token"], db=db)
        assert mine["response"] == "maybe"

        detail = await routes.get_invite(created["id"], db=db, current_user=agent)
        assert detail["counts"]["maybe"] == 1 and detail["counts"]["guests_maybe"] == 2 and len(detail["rsvps"]) == 1

        # email the invitation to a list - example.com only - and remind the ones who haven't answered
        no_send.clear()
        sent = await routes.send_invite(created["id"], InviteSendRequest(recipients=[
            {"email": guest}, {"email": _email("bob"), "name": "Bob"}, {"email": "not-an-email"}]), db=db, current_user=agent)
        assert sent["sent"] == 2 and sent["skipped"][0]["reason"] == "not a valid email"
        await _settle()
        assert [m for m in no_send if m["template"] == "event_invite"]
        again = await routes.resend_invite(created["id"], db=db, current_user=agent)
        assert again["sent"] == 0  # Bob was emailed minutes ago; Jane already answered

        # closed -> no more RSVPs; the page still opens
        await routes.set_status(created["id"], InviteStatusUpdate(status="closed"), db=db, current_user=agent)
        with pytest.raises(HTTPException) as e:
            await routes.public_rsvp(created["code"], RsvpSubmit(response="yes", name="Late", email=_email("late")), request=None, db=db)
        assert e.value.status_code == 422

        dup = await routes.duplicate_invite(created["id"], db=db, current_user=agent)
        assert dup["code"] != created["code"] and dup["status"] == "live"

        # deleting the invite keeps the lead
        lead_id = rs[0].lead_id
        await routes.delete_invite(created["id"], db=db, current_user=agent)
        db.expire_all()
        await db.refresh(agent)
        assert await db.get(Lead, lead_id) is not None
        assert not (await db.execute(select(EventInviteRsvp).where(EventInviteRsvp.invite_id == created["id"]))).scalars().all()


@pytest.mark.asyncio
async def test_brokers_open_rsvp_is_not_a_lead():
    async with AsyncSessionLocal() as db:
        agent = await _agent(db, "broker")
        inv = await routes.create_invite(_save("brokers_open"), db=db, current_user=agent)
        assert inv["audience"] == "agents"
        with pytest.raises(HTTPException):  # brokerage is required
            await routes.public_rsvp(inv["code"], RsvpSubmit(response="yes", name="Al Agent", email=_email("al")), request=None, db=db)
        r = await routes.public_rsvp(inv["code"], RsvpSubmit(response="yes", name="Al Agent", email=_email("al"),
                                     answers=[{"questionId": "brokerage", "value": "Big Realty"}, {"questionId": "party", "value": "2"}]),
                                     request=None, db=db)
        assert r["guest_count"] == 2
        row = (await db.execute(select(EventInviteRsvp).where(EventInviteRsvp.token == r["token"]))).scalar_one()
        assert row.lead_id is None
        assert not (await db.execute(select(Lead).where(Lead.owner_id == agent.id))).scalars().all()


@pytest.mark.asyncio
async def test_public_page_stops_when_subscription_ends_and_other_hosts():
    async with AsyncSessionLocal() as db:
        agent = await _agent(db, "lapsed")
        inv = await routes.create_invite(_save("open_house"), db=db, current_user=agent)
        # another agent's (or no) website domain never serves this invite
        pub = await routes.public_invite(inv["code"], host="someone-else-zz.openhousepal.com", db=db)
        assert pub["host_ok"] is False
        assert (await routes.public_invite(inv["code"], host="localhost:3000", db=db))["host_ok"] is True
        agent.subscription_status = "EXPIRED"
        db.add(agent)
        await db.commit()
        with pytest.raises(HTTPException) as e:
            await routes.public_invite(inv["code"], host=None, db=db)
        assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_past_event_refuses_rsvp():
    async with AsyncSessionLocal() as db:
        agent = await _agent(db, "past")
        inv = await routes.create_invite(_save("custom", event_date=date.today() - timedelta(days=2)), db=db, current_user=agent)
        assert inv["is_past"] is True
        with pytest.raises(HTTPException):
            await routes.public_rsvp(inv["code"], RsvpSubmit(response="yes", name="X", email=_email("x"),
                                     answers=[{"questionId": "party", "value": "2"}]), request=None, db=db)
