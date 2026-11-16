"""11/16, the full editor test before launch: "Schedule it" saved an article as SCHEDULED and nothing ever published
it - every public page shows PUBLISHED articles only, and no job looked at the time. blog_schedule_service.publish_due
is that job's work; main.py runs it every minute.

Uses the local database: three throwaway articles under an existing account, removed afterwards. Only those three
are touched (the statement is narrowed to their ids)."""
import asyncio
import inspect
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.database import AsyncSessionLocal
from app.models.database import Blog, User
from app.services import blog_schedule_service as svc

NOW = datetime(2026, 11, 17, 13, 0, tzinfo=timezone.utc)          # 8:00 AM Eastern


def test_a_scheduled_article_goes_live_at_its_time_and_not_before():
    async def run():
        async with AsyncSessionLocal() as db:
            owner = (await db.execute(select(User.id).limit(1))).scalar()
            if not owner:
                pytest.skip("no account in this database")
            tag = uuid.uuid4().hex[:8]
            first_live = NOW - timedelta(days=30)
            rows = {
                "due": Blog(id=str(uuid.uuid4()), owner_id=owner, slug=f"zz-sched-due-{tag}", title="due", status="SCHEDULED", blocks=[], scheduled_at=NOW - timedelta(seconds=30)),
                "later": Blog(id=str(uuid.uuid4()), owner_id=owner, slug=f"zz-sched-later-{tag}", title="later", status="SCHEDULED", blocks=[], scheduled_at=NOW + timedelta(hours=2)),
                "again": Blog(id=str(uuid.uuid4()), owner_id=owner, slug=f"zz-sched-again-{tag}", title="again", status="SCHEDULED", blocks=[],
                              scheduled_at=NOW - timedelta(minutes=5), published_at=first_live),
                "draft": Blog(id=str(uuid.uuid4()), owner_id=owner, slug=f"zz-sched-draft-{tag}", title="draft", status="DRAFT", blocks=[], scheduled_at=NOW - timedelta(days=1)),
            }
            ids = {k: b.id for k, b in rows.items()}
            db.add_all(rows.values())
            await db.commit()
            try:
                mine = svc.due_statement(NOW).where(Blog.id.in_(list(ids.values())))
                live = await svc.publish_due(db, statement=mine)
                assert sorted(b["id"] for b in live) == sorted([ids["due"], ids["again"]])
                got = {b.id: b for b in (await db.execute(select(Blog).where(Blog.id.in_(list(ids.values()))).execution_options(populate_existing=True))).scalars()}
                due, later, again, draft = (got[ids[k]] for k in ("due", "later", "again", "draft"))
                # due: live, dated the time the agent chose, off the schedule
                assert due.status == "PUBLISHED" and due.scheduled_at is None and due.published_at == NOW - timedelta(seconds=30)
                # not yet: untouched
                assert later.status == "SCHEDULED" and later.scheduled_at == NOW + timedelta(hours=2) and later.published_at is None
                # live once before: keeps its first date
                assert again.status == "PUBLISHED" and again.published_at == first_live
                # a draft that still carries an old time is never published by the job
                assert draft.status == "DRAFT"
                # running it again publishes nothing twice
                assert await svc.publish_due(db, statement=svc.due_statement(NOW).where(Blog.id.in_(list(ids.values())))) == []
                # two hours on, the later one goes
                more = await svc.publish_due(db, statement=svc.due_statement(NOW + timedelta(hours=2)).where(Blog.id.in_(list(ids.values()))))
                assert [b["id"] for b in more] == [ids["later"]]
            finally:
                await db.rollback()
                await db.execute(delete(Blog).where(Blog.id.in_(list(ids.values()))))
                await db.commit()
    asyncio.run(run())


def test_the_job_is_on_the_clock_every_minute():
    from app import main
    src = inspect.getsource(main)
    assert "scheduled_publish_due" in src and 'id="blog_publish_due"' in src and "minutes=1" in src[src.index("_blogs_due, "):src.index('id="blog_publish_due"')]
    # a failure never escapes the job (it is tried again a minute later)
    assert "except Exception" in inspect.getsource(svc.scheduled_publish_due)
