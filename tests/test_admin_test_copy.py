"""Admin "test copy" switch (owner, 10/26) - app/api/admin_routes.set_test_copy."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.admin_routes import set_test_copy


class Db:
    def __init__(self, *users):
        self.users = {u.id: u for u in users}
        self.commits = 0

    async def get(self, _model, uid):
        return self.users.get(uid)

    async def commit(self):
        self.commits += 1


def agent(uid, mls_id=None, copy_of=None, is_admin=False):
    return SimpleNamespace(id=uid, mls_id=mls_id, test_copy_of_user_id=copy_of, broker_authorized=False, is_admin=is_admin)


ADMIN = SimpleNamespace(id="admin")


@pytest.mark.asyncio
async def test_copy_takes_the_agents_mls_id_and_is_approved():
    peter, test = agent("peter", "3372275"), agent("test")
    r = await set_test_copy("test", "peter", db=Db(peter, test), admin=ADMIN)
    assert r["mls_id"] == "3372275" and test.test_copy_of_user_id == "peter" and test.broker_authorized


@pytest.mark.asyncio
async def test_stopping_takes_the_mls_id_off():
    peter, test = agent("peter", "3372275"), agent("test", "3372275", copy_of="peter")
    await set_test_copy("test", "", db=Db(peter, test), admin=ADMIN)
    assert test.mls_id is None and test.test_copy_of_user_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target, source, why", [
    (agent("real", "999"), agent("peter", "3372275"), "own MLS ID"),       # a real agent's ID is never overwritten
    (agent("test"), agent("copy", "3372275", copy_of="peter"), "test copy"),  # copy the real agent, not a copy
    (agent("test"), agent("nomls"), "no MLS ID"),
    (agent("boss", is_admin=True), agent("peter", "3372275"), "admin"),
])
async def test_refused(target, source, why):
    with pytest.raises(HTTPException) as e:
        await set_test_copy(target.id, source.id, db=Db(target, source), admin=ADMIN)
    assert e.value.status_code == 400 and why.split()[-1].lower() in e.value.detail.lower()
