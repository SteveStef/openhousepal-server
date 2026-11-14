"""The agent's own traffic isn't a stat (owner, 11/13): "no stats on the site should count the signed-in
agent - open house sign-in views and completion... but should still count everyone else."
app/utils/own_traffic.py and the tracking endpoints that use it. No database needed."""
import os
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.utils.own_traffic import contact_is_owner, is_own_traffic, not_owner_test

OWNER = SimpleNamespace(id="agent-1", email="Quinn@Example.com")
OTHER = SimpleNamespace(id="agent-2", email="someone@example.com")


def sql(expr) -> str:
    return str(expr.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


# ---------------------------------------------------------------- the rule itself

def test_owner_and_admin_are_own_traffic_everyone_else_counts(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAIL", "boss@example.com")
    assert is_own_traffic(OWNER, "agent-1")
    assert not is_own_traffic(None, "agent-1")            # logged out: a guest
    assert not is_own_traffic(OTHER, "agent-1")           # another agent signed in: still counts
    assert is_own_traffic(SimpleNamespace(id="x", email="Boss@example.com"), "agent-1")   # the site admin
    assert not is_own_traffic(OWNER, None)


def test_own_email_is_a_test():
    assert contact_is_owner(OWNER, " quinn@example.COM ")
    assert not contact_is_owner(OWNER, "guest@example.com")
    assert not contact_is_owner(OWNER, None)
    assert not contact_is_owner(None, "quinn@example.com")


def test_null_flag_counts_as_a_guest():
    from app.models.database import OpenHouseVisitor
    assert sql(not_owner_test(OpenHouseVisitor.owner_test)) == "open_house_visitors.owner_test IS NOT true"


def test_stat_filters_leave_out_tests():
    from app.services.forms_service import real_form_sessions
    from app.services.stats_service import not_owner_lead, real_signin_views
    v = sql(real_signin_views())
    assert "open_house_signin_views.visitor_id IS NULL" in v and "owner_test IS true" in v
    f = sql(real_form_sessions())
    assert "form_view_sessions.submission_id IS NULL" in f and "form_submissions.owner_test IS true" in f
    assert "lower(leads.email) != lower" in sql(not_owner_lead("agent-1"))


# ---------------------------------------------------------------- open house sign-in page

class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeDb:
    def __init__(self, agent_id="agent-1", users=()):
        self.agent_id = agent_id
        self.added = []
        self.users = {u.id: u for u in users}

    async def execute(self, *_a, **_k):
        return _Result(self.agent_id)

    def add(self, row):
        if getattr(row, "id", None) is None and hasattr(row, "id"):
            row.id = f"row-{len(self.added)}"
        self.added.append(row)

    async def commit(self):
        pass

    async def get(self, _model, key):
        return self.users.get(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("viewer, mode, counted", [
    (None, "phone", True),       # a guest's phone
    (OWNER, "phone", False),     # the host scanning their own QR while signed in
    (OWNER, "kiosk", True),      # guests signing in on the host's signed-in tablet
    (OTHER, "phone", True),      # another agent signed in still counts
])
async def test_signin_page_view(viewer, mode, counted):
    from app.api.open_houses_routes import track_signin_view
    db = FakeDb()
    r = await track_signin_view("oh-1", mode=mode, device="mobile", db=db, viewer=viewer)
    assert r["success"] is True
    assert (len(db.added) == 1) is counted
    assert (r["view_id"] is not None) is counted


def _signin(email="guest@example.com", mode="phone"):
    from app.schemas.open_house import OpenHouseFormSubmission
    return OpenHouseFormSubmission(full_name="Jane Guest", email=email, phone="6105550100", has_agent="NO",
                                   open_house_event_id="oh-1", signin_mode=mode)


@pytest.mark.asyncio
@pytest.mark.parametrize("viewer, email, mode, flagged", [
    (None, "guest@example.com", "phone", False),
    (OWNER, "guest@example.com", "phone", True),     # signed in on their own phone
    (OWNER, "guest@example.com", "kiosk", False),    # a real guest on the host's kiosk
    (None, "quinn@example.com", "kiosk", True),      # typed their own email - anywhere
    (OTHER, "guest@example.com", "phone", False),
])
async def test_signin_owner_test_flag(viewer, email, mode, flagged):
    from app.api.open_houses_routes import _flag_owner_test_signin
    visitor = SimpleNamespace(email=email, owner_test=None)
    db = FakeDb(users=[OWNER])
    got = await _flag_owner_test_signin(db, visitor, _signin(email, mode), {"agent_id": "agent-1"}, viewer)
    assert got is flagged
    assert (visitor.owner_test is True) is flagged


# ---------------------------------------------------------------- Agent Hub, invites, Guest Pass

@pytest.mark.asyncio
@pytest.mark.parametrize("viewer, owner_ref, counted", [
    (None, False, True), (OWNER, False, False), (OTHER, False, True), (None, True, False),
])
async def test_hub_visit(monkeypatch, viewer, owner_ref, counted):
    from app.api import agent_hub_routes
    from app.services import hub_slugs

    async def hub_by_slug(_db, _slug):
        return SimpleNamespace(owner_id="agent-1")
    monkeypatch.setattr(hub_slugs, "hub_by_slug", hub_by_slug)
    db = FakeDb()
    await agent_hub_routes.track_agent_hub_event("quinn", agent_hub_routes.AgentHubTrackEvent(event_type="visit"),
                                                 db=db, viewer=viewer, owner_ref=owner_ref)
    assert (len(db.added) == 1) is counted


@pytest.mark.asyncio
@pytest.mark.parametrize("viewer, counted", [(None, True), (OWNER, False), (OTHER, True)])
async def test_invite_view(monkeypatch, viewer, counted):
    from app.api import invites_routes

    async def _public(_db, _code):
        return SimpleNamespace(id="inv-1", owner_id="agent-1"), OWNER
    monkeypatch.setattr(invites_routes, "_public", _public)
    db = FakeDb()
    executed = []

    async def execute(stmt, *_a, **_k):
        executed.append(stmt)
    db.execute = execute
    await invites_routes.public_view("abc", db=db, viewer=viewer, owner_ref=False)
    assert (len(executed) == 1) is counted


@pytest.mark.asyncio
@pytest.mark.parametrize("viewer, counted", [(None, True), (OWNER, False), (OTHER, True)])
async def test_guest_pass_activity(monkeypatch, viewer, counted):
    from app.api import guest_pass_routes as r
    calls = []

    async def resolve(_db, _token):
        return SimpleNamespace(id="v-1"), SimpleNamespace(id="oh-1", agent_id="agent-1"), OWNER

    async def track(*a, **k):
        calls.append("seen")
        return {"noted": True}

    async def ping(*a, **k):
        calls.append("ping")
    monkeypatch.setattr(r.gp, "resolve", resolve)
    monkeypatch.setattr(r.gp, "track", track)
    monkeypatch.setattr(r.gp, "ping", ping)
    await r.guest_pass_seen("t", r.GuestPassSeen(view="map"), db=None, viewer=viewer)
    await r.guest_pass_ping("t", r.GuestPassPing(session="s1"), db=None, viewer=viewer)
    assert calls == (["seen", "ping"] if counted else [])
