"""Owner 11/13: a live article sat on "Gathering" for 15 minutes while the database was overloaded,
then failed - and the stuck run kept going in the background. Every run now ends on its own: a
stalled free first step stops early with a plain "servers busy" answer, the whole run has a time
limit under the progress poll's, nothing stopped is charged, and a run never writes over an article
it no longer owns. No database needed - the session and the pipeline are stand-ins."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.api import blog_routes  # noqa: E402
from app.services.ai import ai_blog_pipeline_service  # noqa: E402

STARTED = "2026-11-13T22:44:12+00:00"


class _Session:
    """Just enough of AsyncSession for _run_blog_ai_job; every session shares one article."""
    def __init__(self, world):
        self.w = world

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, model, _id):
        return self.w["user"] if model.__name__ == "User" else self.w["blog"]

    async def execute(self, stmt):
        self.w["stage_writes"] += 1
        return SimpleNamespace(rowcount=1)

    async def commit(self):
        pass

    async def invalidate(self):
        self.w["invalidated"] = True

    def add(self, obj):
        self.w["added"].append(obj)


@pytest.fixture
def world(monkeypatch):
    w = {"user": SimpleNamespace(id="u1"), "stage_writes": 0, "invalidated": False, "added": [], "refunds": [],
         "blog": SimpleNamespace(id="b1", owner_id="u1", title="Older homes in Wayne", slug="older-homes",
                                 ai_review={"status": "running", "stage": "gathering", "started_at": STARTED, "new": True})}
    import app.database
    monkeypatch.setattr(app.database, "AsyncSessionLocal", lambda: _Session(w))

    async def refund(db, owner_id, since):
        w["refunds"].append((owner_id, since))
    monkeypatch.setattr(ai_blog_pipeline_service, "refund_since", refund)

    async def finished(*a, **k):
        pass
    from app.services.ai import ai_notify
    monkeypatch.setattr(ai_notify, "finished", finished)
    monkeypatch.setattr(blog_routes, "_AI_JOB_POLL_SECONDS", 0.02)
    return w


def _pipeline(stall_in: str, done: dict):
    async def run_pipeline(db, user, brief, *, engine=None, on_stage=None):
        await on_stage("gathering")
        if stall_in != "gathering":
            await on_stage("research")
        try:
            await asyncio.sleep(3600)   # a look-up / model call that never comes back
        except asyncio.CancelledError:
            done["cancelled"] = True
            raise
    return run_pipeline


def test_stalled_gathering_stops_early_refunds_and_says_servers_busy(world, monkeypatch):
    done = {}
    monkeypatch.setattr(blog_routes, "_AI_JOB_STAGE_LIMITS", {"gathering": 0.1})
    monkeypatch.setattr(ai_blog_pipeline_service, "run_pipeline", _pipeline("gathering", done))
    asyncio.run(asyncio.wait_for(blog_routes._run_blog_ai_job("b1", "u1", {"topic": "x"}, "max"), timeout=5))
    review = world["blog"].ai_review
    assert done.get("cancelled"), "the stuck run must be stopped, not left running in the background"
    assert review["status"] == "failed" and "too busy" in review["error"] and "Nothing was charged" in review["error"]
    assert world["refunds"] == [("u1", STARTED)]
    assert world["invalidated"], "the connection that was mid-query is dropped, not reused"
    assert [n.type for n in world["added"]] == ["BLOG_AI_FAILED"]


def test_whole_run_has_a_time_limit_below_the_progress_polls(world, monkeypatch):
    done = {}
    assert blog_routes._AI_JOB_TIME_LIMIT_SECONDS < blog_routes._AI_JOB_STALE_SECONDS
    monkeypatch.setattr(blog_routes, "_AI_JOB_TIME_LIMIT_SECONDS", 0.15)
    monkeypatch.setattr(ai_blog_pipeline_service, "run_pipeline", _pipeline("research", done))
    asyncio.run(asyncio.wait_for(blog_routes._run_blog_ai_job("b1", "u1", {"topic": "x"}, "max"), timeout=5))
    assert done.get("cancelled")
    assert world["blog"].ai_review["status"] == "failed" and "took too long" in world["blog"].ai_review["error"]
    assert world["refunds"] == [("u1", STARTED)]


def test_a_run_never_writes_over_an_article_it_no_longer_owns(world, monkeypatch):
    # The progress poll gave up on it (or the agent started again): the article no longer carries
    # this run's start time, so the late result is dropped - and refunded - instead of written.
    async def run_pipeline(db, user, brief, *, engine=None, on_stage=None):
        await on_stage("gathering")
        world["blog"].ai_review = {"status": "failed", "new": True, "error": "took too long", "brief": {}}
        return {"title": "Late", "usage": {"cost_usd": 0.5}}
    applied = []
    monkeypatch.setattr(ai_blog_pipeline_service, "run_pipeline", run_pipeline)
    monkeypatch.setattr(blog_routes, "_apply_ai_result", lambda blog, result: applied.append(result))
    asyncio.run(asyncio.wait_for(blog_routes._run_blog_ai_job("b1", "u1", {"topic": "x"}, "max"), timeout=5))
    assert applied == [] and world["blog"].ai_review["status"] == "failed"
    assert world["refunds"] == [("u1", STARTED)]
    assert world["added"] == []


def test_a_normal_run_still_finishes_and_rings_the_bell(world, monkeypatch):
    async def run_pipeline(db, user, brief, *, engine=None, on_stage=None):
        await on_stage("gathering")
        await on_stage("research")
        return {"title": "Older homes", "usage": {"cost_usd": 0.42}}
    applied = []
    monkeypatch.setattr(ai_blog_pipeline_service, "run_pipeline", run_pipeline)
    monkeypatch.setattr(blog_routes, "_apply_ai_result", lambda blog, result: applied.append(result))

    async def slug(db, owner, base, exclude_id=None):
        return base
    monkeypatch.setattr(blog_routes, "next_available_slug", slug)
    asyncio.run(asyncio.wait_for(blog_routes._run_blog_ai_job("b1", "u1", {"topic": "x"}, "max"), timeout=5))
    assert applied and world["refunds"] == [] and not world["invalidated"]
    assert world["blog"].ai_review["run_cost_usd"] == 0.42
    assert [n.type for n in world["added"]] == ["BLOG_AI_READY"]
    assert world["stage_writes"] == 2
