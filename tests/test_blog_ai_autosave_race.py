"""Blog test 11/13 (Kennett Square article) - an interview autosave must never flip a running AI job
back to an unfinished draft. The autosave that read the draft a moment before "Write my article"
landed after the job had started: the article read status "interview" for the whole run (Blogs list:
"Unfinished AI draft") and a second Write would have started a second paid run.
Runs against the local Postgres in one rolled-back transaction (skipped when it isn't reachable)."""
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402


def _db_url():
    from dotenv import load_dotenv
    load_dotenv(root_dir / ".env")
    return os.getenv("DATABASE_URL")


class _Db:
    async def __aenter__(self):
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        self.engine = create_async_engine(_db_url())
        try:
            self.conn = await self.engine.connect()
        except Exception as e:  # noqa: BLE001
            await self.engine.dispose()
            pytest.skip(f"local database not reachable: {e}")
        self.trans = await self.conn.begin()
        self.db = AsyncSession(bind=self.conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
        return self.db

    async def __aexit__(self, *exc):
        await self.db.close()
        await self.trans.rollback()
        await self.conn.close()
        await self.engine.dispose()


async def _setup(db):
    from app.models.database import Blog, User
    tag = uuid.uuid4().hex[:8]
    user = User(id=str(uuid.uuid4()), email=f"race-{tag}@example.com", hashed_password="x", first_name="R", last_name="Ace", state="PA", brokerage="Test Realty",
                plan_tier="IDX", subscription_status="ACTIVE")
    db.add(user)
    await db.flush()
    blog = Blog(id=str(uuid.uuid4()), owner_id=user.id, slug=f"race-{tag}", title="Moving to Zqx", status="DRAFT",
                ai_review={"status": "interview", "new": True, "brief": {"topic": "Moving to Zqx"}, "step": 3})
    db.add(blog)
    await db.flush()
    return SimpleNamespace(id=user.id), blog


@pytest.mark.asyncio
async def test_late_autosave_cannot_overwrite_a_running_job():
    from sqlalchemy import select, update
    from app.api.blog_routes import save_blog_ai_brief
    from app.models.database import Blog
    async with _Db() as db:
        user, blog = await _setup(db)
        # The job starts in the database while this session still holds the old "interview" copy -
        # exactly what the late autosave saw.
        await db.execute(update(Blog).where(Blog.id == blog.id).values(
            ai_review={"status": "running", "stage": "write", "new": True, "brief": {"topic": "Moving to Zqx"}}
        ).execution_options(synchronize_session=False))
        assert blog.ai_review["status"] == "interview"   # stale in this session
        with pytest.raises(HTTPException) as e:
            await save_blog_ai_brief({"blog_id": blog.id, "brief": {"topic": "Moving to Zqx"}, "step": 3}, db=db, current_user=user)
        assert e.value.status_code == 409
        row = (await db.execute(select(Blog.ai_review).where(Blog.id == blog.id))).scalar_one()
        assert row["status"] == "running" and row["stage"] == "write"


@pytest.mark.asyncio
async def test_autosave_still_saves_an_unfinished_draft():
    from sqlalchemy import select
    from app.api.blog_routes import save_blog_ai_brief
    from app.models.database import Blog
    async with _Db() as db:
        user, blog = await _setup(db)
        out = await save_blog_ai_brief({"blog_id": blog.id, "brief": {"topic": "Moving to Zqx, PA", "goal": "Decide"}, "step": 2},
                                       db=db, current_user=user)
        assert out["blog_id"] == blog.id
        row = (await db.execute(select(Blog.title, Blog.ai_review).where(Blog.id == blog.id))).one()
        assert row.title == "Moving to Zqx, PA"
        assert row.ai_review["status"] == "interview" and row.ai_review["step"] == 2 and row.ai_review["brief"]["goal"] == "Decide"
