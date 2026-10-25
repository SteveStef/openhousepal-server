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


def _fresh_pool():
    """The app's engine is a module-level singleton whose pooled asyncpg connections belong to the
    event loop that opened them. When another test module (with its own loop) ran first, reusing
    those connections fails with "another operation is in progress" / InterfaceError - so this
    module starts (and leaves) with an empty pool."""
    from app.database import engine
    engine.sync_engine.pool = engine.sync_engine.pool.recreate()


@pytest.fixture(scope="module")
def event_loop():
    _fresh_pool()
    loop = asyncio.new_event_loop()
    yield loop
    _fresh_pool()
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
    return InviteSave(invite_type=kind, title=kw.get("title", p["title"]), subtitle=kw.get("subtitle"), message=p["message"],
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
    # older invites still carry the "working with an agent?" question; the RSVP's own answer wins
    old = svc.clean_questions([*qs, {"id": "agent", "type": "select", "label": "Working with an agent?", "options": ["Yes", "No"], "role": "represented"}])
    assert svc.represented_of(old, {"agent": "No"}) == "no"
    assert svc.represented_of(old, {"agent": "No"}, True) == "yes"
    assert svc.represented_of(qs, {}) is None


def test_lead_kinds():
    inv = EventInvite(audience="clients", questions=[])
    r = EventInviteRsvp(response="yes", answers=[])
    assert svc.lead_kind_of(inv, r) == "lead"
    r.is_agent = True
    assert svc.lead_kind_of(inv, r) == "agent"
    r.is_agent, r.has_agent = False, True
    assert svc.lead_kind_of(inv, r) == "represented"
    r.has_agent, r.response = False, "no"
    assert svc.lead_kind_of(inv, r) == "declined"
    r.still_interested = True
    assert svc.lead_kind_of(inv, r) == "lead"
    assert svc.lead_kind_of(EventInvite(audience="agents", questions=[]), EventInviteRsvp(response="yes", answers=[])) == "agent"


def test_several_days():
    inv = EventInvite(id="x", code="abc", title="Open House", event_date=date(2026, 11, 7), start_time="13:00", end_time="15:00",
                      extra_dates=[{"date": "2026-11-08", "start": "13:00", "end": "15:00"}])
    assert svc.when_label(inv) == "Sat, Nov 7 & Sun, Nov 8 · 1 - 3 PM"
    inv.extra_dates = [{"date": "2026-11-08", "start": "12:00", "end": "14:00"}]
    assert svc.when_label(inv) == "Sat, Nov 7 · 1 - 3 PM / Sun, Nov 8 · 12 - 2 PM"
    text = svc.ics_text(inv, agent_name="Q", url="https://x/i/abc")
    assert text.count("BEGIN:VEVENT") == 2
    only = svc.ics_text(inv, agent_name="Q", url="https://x/i/abc", only=[date(2026, 11, 8)])
    assert only.count("BEGIN:VEVENT") == 1 and "DTSTART:20261108T170000Z" in only
    r = EventInviteRsvp(days=["2026-11-08"])
    assert [d["date"] for d in svc.rsvp_days(inv, r)] == [date(2026, 11, 8)]


def test_designed_emails_render():
    from app.services.invite_email_service import render, render_guest_confirmation
    _, html, text = render("invite_rsvp_agent", "New RSVP", {
        "guest_name": "Jane Guest", "rsvp_response": "yes", "guest_count": 3, "lead_kind": "represented",
        "invite_title": "Open House at 239 Windham Dr", "invite_when": "Saturday, November 7 · 1 - 3 PM",
        "answers": [{"label": "Are you looking to…", "value": "Buy"}, {"label": "Diet", "value": ["Vegan", "None"]}],
        "guest_email": "jane@example.com", "link": "https://x/studio/forms?view=invites", "totals": {"coming": 5, "maybe": 1, "no": 0}})
    assert "Party of 3" in html and "Has an agent" in html and "Vegan, None" in html and "data-ogsc" in html
    assert "Their answers" in html and "jane@example.com" in text
    _, html, text = render("invite_day_before_agent", "Tomorrow", {"invite_title": "Open House", "day_label": "Saturday, November 7 · 1 - 3 PM",
                                                                  "coming": [{"name": "Al", "guest_count": 2, "tag": "new lead", "asked": ""}],
                                                                  "maybe": [], "guests_coming": 2, "link": "https://x"})
    assert "Coming (1)" in html and "Al" in text


def test_personal_templates_render():
    v = {"first_name": "Jo", "agent_name": "Quinn Reviewer", "agent_phone": "(610) 555-0101", "invite_title": "Broker's Open",
         "invite_when": "Tuesday, November 3 · 11 AM - 1 PM", "invite_place": "22 Birch Ln", "invite_link": "https://openhousepal.com/i/abc",
         "Unsub": "https://openhousepal.com/unsubscribe?email=jo%40example.com"}
    subject, html, text = render_personal("event_invite", "Broker's Open", v, link="")
    assert subject == "Broker's Open" and "https://openhousepal.com/i/abc" in html and "Unsubscribe" in html
    # 10/24 wording: "You're on the list - party of 2 - for the open house at ..." with the link behind words
    subject, html, text = render_personal("event_rsvp", "You're on the list", {**v, "rsvp_response": "yes", "guest_count": 2,
                                                                                "event_phrase": "the open house at 239 Windham Dr"}, link="")
    assert "on the list - party of 2 - for the open house at 239 Windham Dr." in text and "https://openhousepal.com/i/abc" in text
    assert ">update your RSVP</a>" in html and ">https://openhousepal.com/i/abc<" not in html
    assert "color-scheme" in html and "data-ogsc" in html


def test_ics_and_when_label():
    inv = EventInvite(id="x", code="abc", title="Dinner, with friends", event_date=date(2026, 11, 7), start_time="18:00", end_time="21:30",
                      place_name="Vetri", address="1312 Spruce St")
    assert svc.when_label(inv) == "Saturday, November 7 · 6 - 9:30 PM"
    text = svc.ics_text(inv, agent_name="Quinn", url="https://openhousepal.com/i/abc")
    assert "DTSTART:20261107T230000Z" in text and "SUMMARY:Dinner\\, with friends" in text and text.endswith("\r\n")


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
        assert {"invite_rsvp_agent", "event_rsvp"} <= templates
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


# ------------------------------------------------------------------ 10/24: lead sorting, several days, requests, day-before


async def _leads(db, owner_id):
    return (await db.execute(select(Lead).where(Lead.owner_id == owner_id).execution_options(populate_existing=True))).scalars().all()


@pytest.mark.asyncio
async def test_rsvp_sorting_and_existing_lead(no_send):
    from app.services.leads_service import upsert_lead
    async with AsyncSessionLocal() as db:
        agent = await _agent(db, "sort")
        aid = agent.id
        inv = await routes.create_invite(_save("open_house"), db=db, current_user=agent)
        party = [{"questionId": "party", "value": "2"}]
        # an agent and someone with an agent: on the guest list + the bell, never a lead
        a = await routes.public_rsvp(inv["code"], RsvpSubmit(response="yes", name="Al Agent", email=_email("al"), is_agent=True, answers=party), request=None, db=db)
        b = await routes.public_rsvp(inv["code"], RsvpSubmit(response="yes", name="Bo Buyer", email=_email("bo"), is_agent=False, has_agent=True, answers=party), request=None, db=db)
        assert not await _leads(db, aid)
        rows = {r.name: r for r in (await db.execute(select(EventInviteRsvp).where(EventInviteRsvp.invite_id == inv["id"]))).scalars().all()}
        assert rows["Al Agent"].lead_kind == "agent" and rows["Bo Buyer"].lead_kind == "represented"
        bell = (await db.execute(select(Notification).where(Notification.agent_id == aid))).scalars().all()
        assert len(bell) == 2 and any("(agent)" in n.title for n in bell)
        # "can't make it" and not interested - just a name is enough, no lead
        await routes.public_rsvp(inv["code"], RsvpSubmit(response="no", name="Cy Nope", still_interested=False), request=None, db=db)
        assert not await _leads(db, aid)
        # an existing lead (phone only) RSVPs with a new email: same person by phone + first name -> no duplicate
        existing = await upsert_lead(db, owner_id=aid, name="Mary Smith", email=None, phone="(610) 555-0111", source_type="open_house_visitor",
                                     source_id=str(uuid.uuid4()), summary="Signed in", occurred_at=datetime.now(timezone.utc))
        mary = _email("mary")
        await routes.public_rsvp(inv["code"], RsvpSubmit(response="no", name="Mary Smith", email=mary, phone="610-555-0111",
                                                          is_agent=False, has_agent=False, still_interested=True), request=None, db=db)
        leads = await _leads(db, aid)
        assert [l.id for l in leads] == [existing.id] and leads[0].email == mary
        events = (await db.execute(select(LeadEvent).where(LeadEvent.lead_id == existing.id))).scalars().all()
        assert {e.source_type for e in events} == {"open_house_visitor", "invite_rsvp"}
        # a real lead (no agent) is a lead
        await routes.public_rsvp(inv["code"], RsvpSubmit(response="yes", name="Dee New", email=_email("dee"), is_agent=False, has_agent=False, answers=party), request=None, db=db)
        assert len(await _leads(db, aid)) == 2
        detail = await routes.get_invite(inv["id"], db=db, current_user=agent)
        assert detail["counts"]["unseen"] == 5 and {r["lead_kind"] for r in detail["rsvps"]} == {"agent", "represented", "declined", "lead"}
        assert a["token"] and b["token"]


@pytest.mark.asyncio
async def test_several_days_requests_and_day_before(no_send):
    async with AsyncSessionLocal() as db:
        agent = await _agent(db, "days")
        aid = agent.id
        tomorrow = (datetime.now(svc.EASTERN) + timedelta(days=1)).date()
        save = _save("open_house", event_date=tomorrow)
        from app.schemas.invites import CoverPosition, InviteDay
        save.extra_dates = [InviteDay(date=tomorrow + timedelta(days=1), start="12:00", end="14:00")]
        save.motion, save.luxe_metal, save.cover_position = "luxe", "silver", CoverPosition(x=50, y=30)
        inv = await routes.create_invite(save, db=db, current_user=agent)
        assert len(inv["dates"]) == 2 and inv["motion"] == "luxe" and inv["accent_color"] == "#A9AFB7" and inv["cover_position"]["y"] == 30
        party = [{"questionId": "party", "value": "3"}]
        r1 = await routes.public_rsvp(inv["code"], RsvpSubmit(response="yes", name="Eve Early", email=_email("eve"), is_agent=False, has_agent=False,
                                                                days=[tomorrow], answers=party), request=None, db=db)
        assert r1["days"] == [tomorrow.isoformat()]
        await routes.public_rsvp(inv["code"], RsvpSubmit(response="yes", name="Lou Later", email=_email("lou"), is_agent=False, has_agent=False,
                                                          days=[tomorrow + timedelta(days=1)], answers=party), request=None, db=db)
        # the Guest Pass's one-tap asks: no open house linked -> still a lead activity + the bell, never twice
        from app.schemas.invites import InvitePublicRequest
        out = await routes.public_request(inv["code"], InvitePublicRequest(kind="disclosure", token=r1["token"]), request=None, db=db)
        assert out == {"sent": True, "already": False}
        again = await routes.public_request(inv["code"], InvitePublicRequest(kind="disclosure", token=r1["token"]), request=None, db=db)
        assert again["already"] is True
        with pytest.raises(HTTPException):
            await routes.public_request(inv["code"], InvitePublicRequest(kind="second_tour", name="No Contact"), request=None, db=db)
        ev = (await db.execute(select(LeadEvent).join(Lead, Lead.id == LeadEvent.lead_id).where(
            Lead.owner_id == aid, LeadEvent.source_type == "open_house_disclosure"))).scalars().all()
        assert len(ev) == 1
        # the agent's "tomorrow" email: once, only tomorrow's guests
        no_send.clear()
        ten_am = datetime.now(svc.EASTERN).replace(hour=10, minute=0)
        assert await svc.send_day_before(db, ten_am, owner_ids=[aid]) == 1
        assert await svc.send_day_before(db, ten_am, owner_ids=[aid]) == 0
        await _settle()
        mail = [m for m in no_send if m["template"] == "invite_day_before_agent"]
        assert len(mail) == 1 and [c["name"] for c in mail[0]["vars"]["coming"]] == ["Eve Early"]
        assert mail[0]["vars"]["coming"][0]["asked"] == "asked for seller disclosure"
        # scheduled open houses for the date step: none for a made-up listing
        assert (await routes.scheduled_for_home(listing_key="ZQ-NONE", address=None, db=db, current_user=agent)) == {"slots": [], "open_house_event_id": None}


@pytest.mark.asyncio
async def test_flyer_line_two(no_send):
    """Round 8: the agent's own line 1 / line 2 are stored exactly as typed; None = automatic, '' = none;
    line 2 rides along in the agent's RSVP email."""
    async with AsyncSessionLocal() as db:
        agent = await _agent(db, "lines")
        save = _save("open_house", title="Sunday Sips & Showing", subtitle="  239 Windham Dr, Exton  ")
        inv = await routes.create_invite(save, db=db, current_user=agent)
        assert inv["title"] == "Sunday Sips & Showing" and inv["subtitle"] == "239 Windham Dr, Exton"
        pub = await routes.public_invite(inv["code"], host=None, db=db)
        assert pub["subtitle"] == "239 Windham Dr, Exton"
        # cleared to "no line 2", then back to automatic
        inv2 = await routes.update_invite(inv["id"], _save("open_house", title="Sunday Sips & Showing", subtitle=""), db=db, current_user=agent)
        assert inv2["subtitle"] == ""
        inv3 = await routes.update_invite(inv["id"], _save("open_house", title="Sunday Sips & Showing", subtitle=None), db=db, current_user=agent)
        assert inv3["subtitle"] is None
        await routes.update_invite(inv["id"], _save("open_house", title="Sunday Sips & Showing", subtitle="Wine & cheese on the porch"), db=db, current_user=agent)
        await routes.public_rsvp(inv["code"], RsvpSubmit(response="yes", name="Lena Line", email=_email("lena"), is_agent=False, has_agent=False, answers=[{"questionId": "party", "value": "2"}]), request=None, db=db)
        await _settle()
        mail = [m for m in no_send if m["template"] == "invite_rsvp_agent"]
        assert mail and mail[-1]["vars"]["invite_subtitle"] == "Wine & cheese on the porch"
