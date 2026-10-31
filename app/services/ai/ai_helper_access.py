"""Who can use the typed AI helper (the bottom-right chat) - owner, 10/30: every paying plan, not
just the admin account, with one Admin switch. Until the owner flips it on, only the admin and the
test accounts below see it.

Voice (Talk to OHP, read-aloud) is off for everyone for now (/ai/helper/status). Which TOOLS a plan
gets is ai_tools.tools_for(user) - a NO_MLS agent never gets an MLS lookup through the helper, same as
the app's own require_mls_access.


"""
from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.database import SystemSettings, User
from app.utils.auth import _assert_active_subscription, get_current_active_user

SETTING_KEY = "ai_helper"   # {"open_to_all": bool}; no row = testers only (owner, 10/30 late)

# While the owner tests it, the helper shows for the admin + these accounts only (the Admin switch
# opens it to every paying agent). Matched by id or login email so it works on any database copy.
TESTER_IDS = {"18fb1af4-e128-4433-bb07-aea2db974bd4"}          # Peter Stefanatos
TESTER_EMAILS = {"peterstefanatos@outlook.com"}


def is_admin(user: User) -> bool:
    return bool(user.is_admin)


async def helper_open_to_all(db: AsyncSession) -> bool:
    row = (await db.execute(select(SystemSettings).where(SystemSettings.key == SETTING_KEY))).scalar_one_or_none()
    return bool((row.value or {}).get("open_to_all", False)) if row else False


def is_tester(user: User) -> bool:
    return user.id in TESTER_IDS or (user.email or "").lower() in TESTER_EMAILS


async def set_helper_open_to_all(db: AsyncSession, on: bool) -> None:
    row = (await db.execute(select(SystemSettings).where(SystemSettings.key == SETTING_KEY))).scalar_one_or_none()
    if row:
        row.value = {**(row.value or {}), "open_to_all": bool(on)}
    else:
        db.add(SystemSettings(key=SETTING_KEY, value={"open_to_all": bool(on)}))
    await db.commit()


async def can_use_helper(db: AsyncSession, user: User) -> bool:
    if is_admin(user) or is_tester(user):
        return True
    try:
        _assert_active_subscription(user)
    except HTTPException:
        return False
    return await helper_open_to_all(db)


async def require_ai_helper_user(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_active_user)) -> User:
    """The typed chat's routes: the admin always; anyone else on an active plan while the Admin
    switch is on."""
    if is_admin(current_user) or is_tester(current_user):
        return current_user
    _assert_active_subscription(current_user)
    if not await helper_open_to_all(db):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="The AI helper is turned off right now.")
    return current_user
