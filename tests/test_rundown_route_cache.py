"""Today's Rundown: requests that arrive together build it once (owner, 10/26)."""
import asyncio
from types import SimpleNamespace

import pytest

from app.api import rundown_routes


@pytest.mark.asyncio
async def test_requests_together_build_once_and_read_drops_it(monkeypatch):
    calls = []

    async def build(_db, user):
        calls.append(user.id)
        await asyncio.sleep(0.05)
        return {"n": len(calls)}

    async def mark(_db, _user):
        return 0

    monkeypatch.setattr(rundown_routes, "build_rundown", build)
    monkeypatch.setattr(rundown_routes, "mark_today_read", mark)
    rundown_routes._recent.clear()
    me = SimpleNamespace(id="agent-1")
    results = await asyncio.gather(*[rundown_routes.todays_rundown(db=None, current_user=me) for _ in range(4)])
    assert calls == ["agent-1"] and all(r == {"n": 1} for r in results)
    await rundown_routes.todays_rundown_read(db=None, current_user=me)
    assert (await rundown_routes.todays_rundown(db=None, current_user=me)) == {"n": 2}
