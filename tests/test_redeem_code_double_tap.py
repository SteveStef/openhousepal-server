"""Sign-up launch audit 11/13 - two taps at once on "Switch on my account" (a billed-by-us team code)
switch the account on once: the code is used once, the second tap is told the account already has a
plan. Both requests used to succeed (code counted twice, two welcome emails). Real local database,
throwaway rows removed afterwards; email/Discord are stubbed."""
import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402


async def _scenario():
    import httpx
    from sqlalchemy import delete, select
    from app.database import AsyncSessionLocal
    from app.main import app as fastapi_app
    from app.models.database import PromoCode, User
    from app.utils.auth import create_access_token
    import app.api.auth_routes as auth_routes
    import app.services.agent_email_service as agent_email_service

    tag = uuid.uuid4().hex[:8]
    user_id = str(uuid.uuid4())
    code = f"DBLTAP-{tag.upper()}"
    try:
        async with AsyncSessionLocal() as db:
            db.add(User(id=user_id, email=f"dbltap-{tag}@example.com", hashed_password="x", first_name="Dee", last_name="Tap",
                        state="PA", brokerage="Test Realty", mls_id=f"DT{tag}", broker_authorized=True,
                        subscription_status="PENDING_PAYMENT"))
            db.add(PromoCode(code=code, kind="billed_by_us", plan_tier="PREMIUM", custom_price=10.0, billing_cycle="monthly", max_uses=5))
            await db.commit()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"local database not reachable: {e}")

    sent = []
    orig_notify, orig_email = auth_routes.notifier.send, agent_email_service.email_agent
    auth_routes.notifier.send = lambda *a, **k: None
    agent_email_service.email_agent = lambda *a, **k: sent.append(a)
    try:
        token = create_access_token({"sub": user_id})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://test") as c:
            r1, r2 = await asyncio.gather(*[c.post("/auth/redeem-code", json={"code": code},
                                                   headers={"Authorization": f"Bearer {token}"}) for _ in range(2)])
        async with AsyncSessionLocal() as db:
            uses = (await db.execute(select(PromoCode.uses).where(PromoCode.code == code))).scalar_one()
            u = await db.get(User, user_id)
            return sorted([r1.status_code, r2.status_code]), uses, u.plan_tier, u.subscription_status, len(sent)
    finally:
        auth_routes.notifier.send, agent_email_service.email_agent = orig_notify, orig_email
        async with AsyncSessionLocal() as db:
            await db.execute(delete(User).where(User.id == user_id))
            await db.execute(delete(PromoCode).where(PromoCode.code == code))
            await db.commit()


def test_two_taps_switch_the_account_on_once():
    codes, uses, tier, status, emails = asyncio.run(_scenario())
    assert codes == [200, 400]
    assert uses == 1
    assert (tier, status) == ("PREMIUM", "ACTIVE")
    assert emails == 1
