"""A follow-up only mentions the guest's own agent when they said they have one (owner, 11/19).

Janet signed in at 417 Dogwood Ln saying she has NO agent, and "Write it for me" still wrote "If you or your
agent have any questions about the home". "No" and unknown read the same: no agent."""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import tasks_service as ts

JANET = ("Hi Janet, it's Peter! Thanks again for coming to my open house at 417 Dogwood Ln {oh_when:2026-10-25}. "
         "If you or your agent have any questions about the home, I'm happy to help with whatever you need.")


@pytest.mark.parametrize("represented", ["no", None, "unknown", ""])
def test_no_agent_or_unknown_drops_the_agent(represented):
    out = ts.without_their_agent(JANET, represented)
    assert "agent" not in out.lower()
    assert "If you have any questions about the home, I'm happy to help with whatever you need." in out
    assert out.startswith("Hi Janet, it's Peter! Thanks again for coming to my open house at 417 Dogwood Ln {oh_when:2026-10-25}.")


def test_has_agent_keeps_it():
    assert ts.without_their_agent(JANET, "yes") == JANET


def test_other_shapes():
    f = lambda m: ts.without_their_agent(m, "no")
    assert f("Do you or your agent have any questions?") == "Do you have any questions?"
    assert f("You and your agent are welcome to come back.") == "You are welcome to come back."
    assert f("You or your Realtor can call me anytime.") == "You can call me anytime."
    assert f("Hi Ann! Loved meeting you. Feel free to pass this along to your agent. Talk soon!") == "Hi Ann! Loved meeting you. Talk soon!"
    assert f("Hi Ann! Any questions for you or your buyer's agent, just text me.") == "Hi Ann! Any questions for you, just text me."
    # never touched: the agent talking about themself, the Agent Hub, a message with no agent in it
    same = ["I'm the listing agent, so text me anytime.", "It's all on my Agent Hub.", "Hi Ann, how's the search going?"]
    assert [f(m) for m in same] == same
    # the signature, the Showcase marker and blank lines stay where they were
    msg = "Hi Ann! Any questions, you or your agent can text me.\n\n" + ts.SHOWCASE_MARKER + "\n\nThanks,\nPeter | COMPASS"
    assert f(msg) == "Hi Ann! Any questions, you can text me.\n\n" + ts.SHOWCASE_MARKER + "\n\nThanks,\nPeter | COMPASS"


# ---- the real "Write it for me" path on the local dev database, AI answer faked (no spend) ----

class _FakeClient:
    def __init__(self, text):
        self.messages = SimpleNamespace(create=self._create)
        self._text = text

    async def _create(self, **kw):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._text)],
                               usage=SimpleNamespace(input_tokens=10, output_tokens=10))


@pytest.mark.asyncio
async def test_write_it_for_me_three_leads():
    import json
    import app.main  # noqa: F401
    from sqlalchemy import delete, select
    from app.database import AsyncSessionLocal
    from app.models.database import AgentTask, Lead, LeadEvent, OpenHouseEvent, User
    from app.services import followup_writer as fw
    from app.services.ai import ai_home_reader as reader
    from app.services.ai import ai_rate_limit_service as rl
    from app.services.ai.ai_actions_catalog import _p_new_task

    ai_text = json.dumps({"title": "Text Janet - check in on 417 Dogwood Ln", "link": False, "why": "check in",
                          "message": "Hi Janet, it's Peter! Thanks again for coming to my open house at 417 Dogwood Ln {when}. "
                                     "If you or your agent have any questions about the home, I'm happy to help with whatever you need."})

    async def _room(db, user, config):
        return None, 0

    async def _reserve(*a, **k):
        return None

    async def _noop(*a, **k):
        return None

    tag = f"zqagent-{uuid.uuid4().hex[:8]}"
    came = datetime.now(timezone.utc) - timedelta(days=25)
    try:
        async with AsyncSessionLocal() as db:
            agent = User(email=f"{tag}@example.com", hashed_password="x", first_name="Peter", last_name="Stefan", state="PA",
                         brokerage="COMPASS", mls_id=tag.upper(), plan_tier="BASIC", subscription_status="ACTIVE")
            db.add(agent)
            await db.commit()
            oh = OpenHouseEvent(agent_id=agent.id, qr_code=tag, address="417 Dogwood Ln, Coatesville, PA 19320",
                                scheduled_date=ts._eastern_day(came))
            db.add(oh)
            await db.commit()
            leads = {}
            for rep in ("no", "yes", None):
                lead = Lead(owner_id=agent.id, name=f"Janet {rep or 'unknown'}case", email=f"{tag}-{rep}@example.com",
                            phone="6105550123", represented=rep, status="new", tags=[])
                db.add(lead)
                await db.commit()
                db.add(LeadEvent(lead_id=lead.id, source_type="open_house_visitor", open_house_event_id=oh.id,
                                 property_address="417 DOGWOOD LN", occurred_at=came, summary="Signed in at 417 Dogwood Ln"))
                await db.commit()
                leads[rep] = lead

            with patch.object(reader, "_get_client", lambda: _FakeClient(ai_text)), patch.object(reader, "room", _room), \
                    patch.object(rl, "reserve_global_and_emergency", _reserve), patch.object(rl, "record_usage_event", _noop), \
                    patch.object(rl, "reconcile_budget", _noop):
                got = {rep: await fw.write(db, agent, lead) for rep, lead in leads.items()}
            for rep, out in got.items():
                print(f"\n[{rep or 'unknown'}] {out['preview']}")
            for rep in ("no", None):
                assert "agent" not in got[rep]["preview"].lower(), got[rep]["preview"]
                assert "If you have any questions about the home" in got[rep]["preview"]
            assert "If you or your agent have any questions about the home" in got["yes"]["preview"]

            # The AI chat's "add a follow-up" draft is cleaned the same way.
            draft = "Hi Janet! If you or your agent have any questions about the home, just text me."
            for rep, lead in leads.items():
                prepared = await _p_new_task(db, agent, {"title": "Text Janet", "kind": "text", "lead_id": lead.id, "suggested_message": draft})
                saved = prepared.payload["body"]["suggested_message"]
                assert saved == (draft if rep == "yes" else "Hi Janet! If you have any questions about the home, just text me."), (rep, saved)
    finally:
        async with AsyncSessionLocal() as db:
            ids = (await db.execute(select(User.id).where(User.email.like(f"{tag}%")))).scalars().all()
            if ids:
                lead_ids = (await db.execute(select(Lead.id).where(Lead.owner_id.in_(ids)))).scalars().all()
                await db.execute(delete(AgentTask).where(AgentTask.owner_id.in_(ids)))
                if lead_ids:
                    await db.execute(delete(LeadEvent).where(LeadEvent.lead_id.in_(lead_ids)))
                await db.execute(delete(Lead).where(Lead.owner_id.in_(ids)))
                await db.execute(delete(OpenHouseEvent).where(OpenHouseEvent.agent_id.in_(ids)))
                await db.execute(delete(User).where(User.id.in_(ids)))
                await db.commit()


# ---- "has an agent" comes from ANY of their answers, not only the lead page (owner, 11/19) ----

@pytest.mark.asyncio
async def test_has_own_agent_reads_every_answer():
    import app.main  # noqa: F401
    from sqlalchemy import delete, select
    from app.database import AsyncSessionLocal
    from app.models.database import Lead, LeadEvent, OpenHouseEvent, OpenHouseVisitor, User
    tag = f"zqagsrc-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    form_yes = {"answers": [{"questionId": "sc_has_agent", "label": "Are you working with an agent?", "value": "Yes"}]}
    form_seller_yes = {"answers": [{"questionId": "sell_working_with_agent", "value": "Yes, I am"}]}
    try:
        async with AsyncSessionLocal() as db:
            agent = User(email=f"{tag}@example.com", hashed_password="x", first_name="Peter", last_name="Stefan", state="PA",
                         brokerage="COMPASS", mls_id=tag.upper(), plan_tier="BASIC", subscription_status="ACTIVE")
            db.add(agent)
            await db.commit()
            oh = OpenHouseEvent(agent_id=agent.id, qr_code=tag, address="417 Dogwood Ln, Coatesville, PA 19320", scheduled_date=now.date())
            db.add(oh)
            await db.commit()

            async def lead(name, rep):
                l = Lead(owner_id=agent.id, name=name, email=f"{tag}-{name.split()[0].lower()}@example.com", phone="6105550123",
                         represented=rep, status="new", tags=[])
                db.add(l)
                await db.commit()
                return l

            def visit(l, has, when):
                db.add(OpenHouseVisitor(full_name=l.name, email=l.email, phone=l.phone, has_agent=has, open_house_event_id=oh.id,
                                        qr_code=tag, lead_id=l.id, created_at=when))

            def form(l, snap, when):
                db.add(LeadEvent(lead_id=l.id, source_type="form_submission", occurred_at=when, summary="Filled out a form", raw_snapshot=snap))

            # said NO at the open house, then YES on a buyer form later: has an agent (lead page still says no)
            a = await lead("Ann Laterform", "no"); visit(a, "NO", now - timedelta(days=20)); form(a, form_yes, now - timedelta(days=5))
            # lead page unknown, open house sign-in said YES
            b = await lead("Bea Signin", "unknown"); visit(b, "YES", now - timedelta(days=3))
            # a seller form said yes
            c = await lead("Cal Seller", "unknown"); form(c, form_seller_yes, now - timedelta(days=2))
            # said yes long ago, NO at the latest open house: no agent now
            d = await lead("Dee Changed", "unknown"); form(d, form_yes, now - timedelta(days=60)); visit(d, "NO", now - timedelta(days=1))
            # Janet: no, and nothing else
            e = await lead("Janet Plain", "no"); visit(e, "NO", now - timedelta(days=25))
            # nobody ever asked
            f = await lead("Fay Unknown", "unknown")
            # the agent marked them "Has an agent" on the lead page
            g = await lead("Gus Marked", "yes")
            await db.commit()
            got = {l.name.split()[0]: await ts.has_own_agent(db, l) for l in (a, b, c, d, e, f, g)}
            assert got == {"Ann": True, "Bea": True, "Cal": True, "Dee": False, "Janet": False, "Fay": False, "Gus": True}, got
    finally:
        async with AsyncSessionLocal() as db:
            ids = (await db.execute(select(User.id).where(User.email.like(f"{tag}%")))).scalars().all()
            if ids:
                lead_ids = (await db.execute(select(Lead.id).where(Lead.owner_id.in_(ids)))).scalars().all()
                await db.execute(delete(OpenHouseVisitor).where(OpenHouseVisitor.qr_code == tag))
                if lead_ids:
                    await db.execute(delete(LeadEvent).where(LeadEvent.lead_id.in_(lead_ids)))
                await db.execute(delete(Lead).where(Lead.owner_id.in_(ids)))
                await db.execute(delete(OpenHouseEvent).where(OpenHouseEvent.agent_id.in_(ids)))
                await db.execute(delete(User).where(User.id.in_(ids)))
                await db.commit()
