"""Accuracy-test 'arms': run the REAL agent CMA wizard (in a browser, as the test agent) on a home
that already sold, with the server showing that home as it stood the day before it went under
contract.

The admin test runner creates an arm for (test agent account, sold listing). While it is live:
  * the wizard's address search shows that home as Active at its as-of asking price;
  * the listing lookup returns the as-of view (status, asking price, days on market, remarks);
  * the comp search shifts the agent's "sold within N days" window to end on the as-of date;
  * saving the subject flags the report as an accuracy-test report (never listed to agents);
  * the AI generation runs inside the time machine (cma_time_machine) - nothing after the as-of
    date reaches any model, and every model input is leak-scanned.
Nothing changes for any owner/listing without a live arm, and only the admin runner creates arms.
"""
from __future__ import annotations

import contextlib
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import CmaBacktestArm
from app.services import cma_time_machine

ARM_TTL_HOURS = 3


def address_key(street: Optional[str], zipcode: Optional[str] = None) -> str:
    s = " ".join(str(street or "").upper().replace(".", "").replace(",", " ").split())
    return f"{s}|{str(zipcode or '')[:5]}"


def _street_of(address: str) -> str:
    return " ".join(str(address or "").split(",")[0].upper().replace(".", "").split())


async def live_arms(db: AsyncSession, owner_id: str) -> list[CmaBacktestArm]:
    now = datetime.now(timezone.utc)
    rows = (await db.execute(select(CmaBacktestArm).where(
        CmaBacktestArm.owner_id == owner_id, CmaBacktestArm.finished_at.is_(None), CmaBacktestArm.expires_at > now,
    ))).scalars().all()
    return list(rows)


async def find_arm(db: AsyncSession, owner_id: Optional[str], *, listing_key: Optional[str] = None,
                   address: Optional[str] = None, report_id: Optional[str] = None) -> Optional[CmaBacktestArm]:
    if not owner_id:
        return None
    arms = await live_arms(db, owner_id)
    for a in arms:
        if report_id and a.report_id == report_id:
            return a
    for a in arms:
        if listing_key and a.listing_key == str(listing_key):
            return a
    if address:
        street = _street_of(address)
        for a in arms:
            if a.address_key and a.address_key.split("|")[0] == street:
                return a
    return None


def tm_from_arm(arm: CmaBacktestArm) -> cma_time_machine.TimeMachine:
    f = arm.facts or {}
    d = lambda v: date.fromisoformat(str(v)[:10]) if v else None  # noqa: E731
    return cma_time_machine.TimeMachine(
        listing_key=arm.listing_key, as_of=d(f["as_of"]), list_price=f.get("list_price"), dom=f.get("dom_as_of"),
        close_price=f.get("close_price"), close_date=d(f.get("close_date")), contract_date=d(f.get("contract_date")),
    )


@contextlib.contextmanager
def activated(arm: Optional[CmaBacktestArm]):
    if arm is None:
        yield None
        return
    tm = tm_from_arm(arm)
    token = cma_time_machine.activate(tm)
    try:
        yield tm
    finally:
        cma_time_machine.deactivate(token)


def mask_lookup(detail, arm: CmaBacktestArm):
    """The listing-lookup response (PropertyDetailResponse) as it read on the as-of date."""
    f = arm.facts or {}
    upd = {"home_status": "ACTIVE-BRIGHT", "price": f.get("list_price"), "days_on_market": f.get("dom_as_of"),
           "price_change_timestamp": None}
    if hasattr(detail, "cumulative_days_on_market"):
        upd["cumulative_days_on_market"] = f.get("dom_as_of")
    if getattr(detail, "description", None):
        upd["description"], _ = cma_time_machine.scrub_remarks(detail.description)
    living = getattr(detail, "living_area", None)
    if f.get("list_price") and living:
        upd["price_per_square_feet"] = round(float(f["list_price"]) / float(living), 2)
    return detail.model_copy(update={k: v for k, v in upd.items() if hasattr(detail, k)})


def mask_suggestion(row: dict, arm: CmaBacktestArm) -> dict:
    f = arm.facts or {}
    return {**row, "status": "ACTIVE-BRIGHT", "homeStatus": "ACTIVE-BRIGHT", "MlsStatus": "ACTIVE-BRIGHT",
            "price": f.get("list_price"), "ListPrice": f.get("list_price")}


def shift_search_window(req, arm: CmaBacktestArm):
    """The agent's 'sold within N days' (counted from today) becomes N days ending on the as-of date."""
    as_of = date.fromisoformat(arm.facts["as_of"])
    today = date.today()
    span = None
    if req.sale_date_from:
        try:
            span = (today - date.fromisoformat(str(req.sale_date_from)[:10])).days
        except ValueError:
            span = None
    upd = {"sale_date_to": as_of.isoformat()}
    if span is not None:
        upd["sale_date_from"] = (as_of - timedelta(days=max(span, 0))).isoformat()
    return req.model_copy(update=upd)


async def create_arm(db: AsyncSession, *, owner_id: str, item: dict, batch_id: Optional[str], item_index: Optional[int],
                     dry_run: bool) -> CmaBacktestArm:
    # One live arm per owner at a time - the wizard can only build one test report at once.
    for a in await live_arms(db, owner_id):
        a.finished_at = datetime.now(timezone.utc)
    arm = CmaBacktestArm(
        owner_id=owner_id, batch_id=batch_id, item_index=item_index, listing_key=item["listing_key"],
        address_key=address_key(item.get("address"), item.get("zipcode")), dry_run=dry_run,
        facts={k: item.get(k) for k in ("as_of", "as_of_source", "list_price", "dom_as_of", "list_date",
                                         "close_price", "close_date", "contract_date", "address", "city", "zipcode")},
        expires_at=datetime.now(timezone.utc) + timedelta(hours=ARM_TTL_HOURS),
    )
    db.add(arm)
    await db.commit()
    await db.refresh(arm)
    return arm
