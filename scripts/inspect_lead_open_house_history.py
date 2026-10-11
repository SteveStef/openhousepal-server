import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models.database import Lead, LeadEvent, OpenHouseEvent


async def main() -> None:
    async with AsyncSessionLocal() as db:
        stmt = (
            select(OpenHouseEvent.id, OpenHouseEvent.address, func.count(func.distinct(LeadEvent.lead_id)).label("guests"))
            .join(LeadEvent, LeadEvent.open_house_event_id == OpenHouseEvent.id)
            .join(Lead, Lead.id == LeadEvent.lead_id)
            .where(LeadEvent.source_type == "open_house_visitor")
            .group_by(OpenHouseEvent.id, OpenHouseEvent.address)
            .order_by(OpenHouseEvent.created_at.desc())
            .limit(20)
        )
        rows = (await db.execute(stmt)).all()
        print(f"history_options={len(rows)}")
        for event_id, address, guests in rows:
            print(f"{event_id} | guests={guests} | {address}")


if __name__ == "__main__":
    asyncio.run(main())
