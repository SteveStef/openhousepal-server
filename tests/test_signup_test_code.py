"""Test-account sign-up codes (owner, 10/26): a Bright sign-up may take a real agent's MLS ID only
with an admin code made for that agent, and the free plan behind it only works for that test copy."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import auth_routes
from app.schemas.user import UserCreate
from app.services import promo_code_service

PETER = SimpleNamespace(id="peter", mls_id="3372275", test_copy_of_user_id=None, first_name="Peter", last_name="Stefanatos")
CODE = SimpleNamespace(id="c1", code="LORITEST", label=None, kind="billed_by_us", plan_tier="IDX", test_copy_of_user_id="peter",
                       billing_group_id=None, billing_cycle="monthly", custom_price=None)
PLAIN = SimpleNamespace(id="c2", code="SMITHTEAM", kind="billed_by_us", plan_tier="IDX", test_copy_of_user_id=None,
                        billing_group_id=None, billing_cycle="monthly", custom_price=79.0)


class Db:
    def __init__(self, *users):
        self.users = {u.id: u for u in users}
        self.commits = 0

    async def get(self, _model, uid):
        return self.users.get(uid)

    async def commit(self):
        self.commits += 1

    async def refresh(self, _obj):
        pass

    async def rollback(self):
        pass


def _codes(monkeypatch, *codes):
    by_code = {c.code: c for c in codes}

    async def find_usable(_db, code):
        row = by_code.get(promo_code_service.clean_code(code))
        return (row, "") if row else (None, "That code isn't valid. Check the spelling and try again.")
    monkeypatch.setattr(promo_code_service, "find_usable", find_usable)


@pytest.mark.asyncio
async def test_code_unlocks_the_copied_agents_mls_id(monkeypatch):
    _codes(monkeypatch, CODE)
    source, promo, why = await promo_code_service.test_copy_source(Db(PETER), "loritest", " 3372275 ")
    assert source is PETER and promo is CODE and why == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("code, mls_id, why", [
    ("LORITEST", "1234567", "different MLS ID"),     # the code is tied to one agent's ID
    ("SMITHTEAM", "3372275", "isn't a test account code"),  # an ordinary code unlocks nothing
    ("NOPE", "3372275", "isn't valid"),
])
async def test_code_refused(monkeypatch, code, mls_id, why):
    _codes(monkeypatch, CODE, PLAIN)
    source, promo, reason = await promo_code_service.test_copy_source(Db(PETER), code, mls_id)
    assert source is None and promo is None and why in reason


@pytest.mark.asyncio
async def test_code_for_a_copy_is_refused(monkeypatch):
    copy = SimpleNamespace(id="peter", mls_id="3372275", test_copy_of_user_id="someone")
    _codes(monkeypatch, CODE)
    source, _, reason = await promo_code_service.test_copy_source(Db(copy), "LORITEST", "3372275")
    assert source is None and "test account code" in reason


def test_signup_code_only_kept_for_bright():
    base = dict(email="a@b.com", first_name="Lori", last_name="D'Onofrio", state="PA", brokerage="COMPASS RE", password="x" * 8)
    assert UserCreate(**base, mls_id="3372275", signup_code=" LORITEST ").signup_code == "LORITEST"
    assert UserCreate(**base, is_bright=False, license_number="RS1", signup_code="LORITEST").signup_code is None


@pytest.mark.asyncio
async def test_free_test_plan_only_for_its_test_copy(monkeypatch):
    _codes(monkeypatch, CODE)
    stranger = SimpleNamespace(id="x", mls_id="999", test_copy_of_user_id=None, subscription_id=None,
                               subscription_status="PENDING_PAYMENT", plan_tier=None, broker_authorized=True)
    with pytest.raises(HTTPException) as e:
        await auth_routes.redeem_billed_by_us_code({"code": "LORITEST"}, db=Db(), current_user=stranger)
    assert e.value.detail == auth_routes.TEST_CODE_ONLY
    with pytest.raises(HTTPException) as e:
        await auth_routes.verify_bundle_code({"code": "LORITEST"}, db=_LookupDb(), current_user=stranger)
    assert e.value.detail == auth_routes.TEST_CODE_ONLY


@pytest.mark.asyncio
async def test_checkout_shows_the_test_copy_a_free_plan(monkeypatch):
    _codes(monkeypatch, CODE)
    lori = SimpleNamespace(id="lori", mls_id="3372275", test_copy_of_user_id="peter", broker_authorized=True)
    r = await auth_routes.verify_bundle_code({"code": "LORITEST"}, db=_LookupDb(), current_user=lori)
    assert r["test_account"] is True and r["kind"] == "billed_by_us" and r["plan_tier"] == "IDX"


class _LookupDb(Db):
    async def execute(self, _stmt):
        return SimpleNamespace(first=lambda: ("c1",))
