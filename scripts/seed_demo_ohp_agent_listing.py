"""One-off dev helper: creates a fake OpenHousePal user (a pretend listing agent) and a synthetic
Property row owned by that agent, positioned/filtered so it shows up in a specific tester's current
Discovery search. Lets that tester click "Request to Host" on a listing where AgentOnPlatform=True,
exercising the normal in-app request flow instead of the email fallback. Also gives that same demo
agent a real, working login (test123@test.com / test123) on an active PREMIUM plan, broker-authorized,
so the tester can log in AS the listing agent and see the host-request/notification flow from that
side too. Pure direct DB inserts - does not touch the Bright MLS API.

Usage: python scripts/seed_demo_ohp_agent_listing.py
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.database import User, Property
from app.utils.auth import hash_password

DEMO_AGENT_EMAIL = "test123@test.com"
DEMO_AGENT_PASSWORD = "test123"
DEMO_AGENT_MLS_ID = "TEST-DEMO-0001"
DEMO_LISTING_KEY = "TEST-DISCOVERY-DEMO-0001"

# Stand-in listing photos (Unsplash) - real estate exterior/interior shots, not the real property.
DEMO_PHOTOS = [
    "https://images.unsplash.com/photo-1570129477492-45c003edd2be?w=1200&q=80",
    "https://images.unsplash.com/photo-1600596542815-ffad4c1539a9?w=1200&q=80",
    "https://images.unsplash.com/photo-1600585154340-be6161a56a0c?w=1200&q=80",
    "https://images.unsplash.com/photo-1600607687939-ce8a6c25118c?w=1200&q=80",
]


async def main():
    async with AsyncSessionLocal() as db:
        # 1. Fake listing agent - has a real, loggable-in OpenHousePal account so AgentOnPlatform
        # resolves True AND the tester can log in as this agent to see the receiving side of the
        # host-request flow. Looked up by mls_id (stable) rather than email, since the email/login
        # itself is one of the fields this script sets/refreshes.
        result = await db.execute(select(User).where(User.mls_id == DEMO_AGENT_MLS_ID))
        demo_agent = result.scalar_one_or_none()
        now_ts = datetime.now(timezone.utc)
        if not demo_agent:
            demo_agent = User(
                id=str(uuid.uuid4()),
                email=DEMO_AGENT_EMAIL,
                hashed_password=hash_password(DEMO_AGENT_PASSWORD),
                first_name="Jordan",
                last_name="Rivera (Test Agent)",
                state="PA",
                brokerage="Compass Pennsylvania, LLC",
                mls_id=DEMO_AGENT_MLS_ID,
                broker_authorized=True,
                plan_tier="PREMIUM",
                subscription_status="ACTIVE",
                subscription_started_at=now_ts,
                trial_ends_at=now_ts + timedelta(days=365),
            )
            db.add(demo_agent)
            await db.flush()
            print(f"Created demo agent user: {demo_agent.first_name} {demo_agent.last_name} (mls_id={demo_agent.mls_id})")
        else:
            demo_agent.email = DEMO_AGENT_EMAIL
            demo_agent.hashed_password = hash_password(DEMO_AGENT_PASSWORD)
            demo_agent.broker_authorized = True
            demo_agent.plan_tier = "PREMIUM"
            demo_agent.subscription_status = "ACTIVE"
            print(f"Demo agent user already existed (mls_id={demo_agent.mls_id}) - refreshed login/plan")
        print(f"Login: {DEMO_AGENT_EMAIL} / {DEMO_AGENT_PASSWORD}")

        # 2. Fake listing owned by that agent - Villanova/PA/Compass Pennsylvania LLC/ACTIVE so it
        # matches the same landmark radius + state + brokerage filters already saved on the
        # tester's Discovery preferences (612 Radnor Valley Drive Villanova, PA, 10mi, brokerage
        # COMPASS).
        # mls_list_date=now makes the response compute DaysOnMarket=0 / New=True, so the demo
        # listing sorts to the front under the default "newest first" Discovery sort.
        now = datetime.now(timezone.utc)

        result = await db.execute(select(Property).where(Property.listing_key == DEMO_LISTING_KEY))
        demo_property = result.scalar_one_or_none()
        if not demo_property:
            demo_property = Property(
                id=str(uuid.uuid4()),
                listing_key=DEMO_LISTING_KEY,
                listing_id="TEST-0001",
                street_address="123 Demo Test Lane",
                unparsed_address="123 Demo Test Lane, Villanova, PA 19085",
                full_address="123 Demo Test Lane, Villanova, PA 19085",
                city="VILLANOVA",
                state="PA",
                zipcode="19085",
                price=1450000,
                bedrooms=5,
                bathrooms=4,
                living_area=3400,
                home_status="ACTIVE-BRIGHT",
                home_type="SINGLE_FAMILY",
                mls_property_type="Residential",
                latitude=40.058,
                longitude=-75.318,
                img_src=DEMO_PHOTOS[0],
                photos=DEMO_PHOTOS,
                description="TEST LISTING - not a real property. Used to test the Discovery 'Request to Host' flow against an agent who has an OpenHousePal account.",
                list_agent_full_name="Jordan Rivera (Test Agent)",
                list_agent_email=DEMO_AGENT_EMAIL,
                list_office_name="Compass Pennsylvania, LLC",
                list_office_phone="555-010-0100",
                list_agent_mls_id=DEMO_AGENT_MLS_ID,
                list_agent_preferred_phone="555-010-0101",
                is_manual=True,
                mls_list_date=now,
                days_on_market=0,
            )
            db.add(demo_property)
            print(f"Created demo property listing_key={DEMO_LISTING_KEY} at {demo_property.street_address}")
        else:
            demo_property.mls_list_date = now
            demo_property.days_on_market = 0
            demo_property.home_status = "ACTIVE-BRIGHT"
            demo_property.price = 1450000
            demo_property.bedrooms = 5
            demo_property.bathrooms = 4
            demo_property.living_area = 3400
            demo_property.img_src = DEMO_PHOTOS[0]
            demo_property.photos = DEMO_PHOTOS
            print(f"Demo property already existed (listing_key={DEMO_LISTING_KEY}) - refreshed mls_list_date/price/photos so it shows as New again")

        await db.commit()
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
