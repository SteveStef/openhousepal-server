"""One-off dev helper: creates a synthetic $2M/5,000 sqft Villanova, PA listing owned by Peter
Stefanatos's real OpenHousePal account, and points the existing "John Doe" test account
(test123@test.com) at it via Discovery preferences, so the Discovery -> Request to Host -> Open
House Manager flow can be exercised end-to-end between two real accounts. Also resets the John Doe
test account's password to a known value so it can be logged into for testing.

Pure direct DB inserts - does not touch the Bright MLS API.

Usage: python scripts/seed_peter_villanova_listing.py
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.database import User, Property, DiscoveryPreferences
from app.utils.auth import hash_password

PETER_MLS_ID = "3372275"
JOHN_MLS_ID = "TEST-DEMO-0001"
JOHN_TEST_PASSWORD = "test123"

LISTING_KEY = "PETER-VILLANOVA-2M-DEMO-0001"

# Villanova, PA landmark coords (matches Peter's own saved Discovery landmark).
VILLANOVA_LAT = 40.0186421
VILLANOVA_LON = -75.3665373

DEMO_PHOTO = "https://images.unsplash.com/photo-1613977257363-707ba9348227?w=1200&q=80"


async def main():
    async with AsyncSessionLocal() as db:
        peter = (await db.execute(select(User).where(User.mls_id == PETER_MLS_ID))).scalar_one()
        john = (await db.execute(select(User).where(User.mls_id == JOHN_MLS_ID))).scalar_one()

        # 1. Reset John Doe test account's password to a known value for login testing.
        john.hashed_password = hash_password(JOHN_TEST_PASSWORD)
        print(f"John Doe test login: {john.email} / {JOHN_TEST_PASSWORD}")

        # 2. Peter's new listing.
        now = datetime.now(timezone.utc)
        result = await db.execute(select(Property).where(Property.listing_key == LISTING_KEY))
        listing = result.scalar_one_or_none()
        if not listing:
            listing = Property(
                id=str(uuid.uuid4()),
                listing_key=LISTING_KEY,
                listing_id="DEMO-2M-VNOVA",
                street_address="456 Demo Estate Drive",
                unparsed_address="456 Demo Estate Drive, Villanova, PA 19085",
                full_address="456 Demo Estate Drive, Villanova, PA 19085",
                city="VILLANOVA",
                state="PA",
                zipcode="19085",
                price=2000000,
                bedrooms=5,
                bathrooms=5.5,
                living_area=5000,
                home_status="ACTIVE-BRIGHT",
                home_type="SINGLE_FAMILY",
                mls_property_type="Residential",
                latitude=VILLANOVA_LAT,
                longitude=VILLANOVA_LON,
                img_src=DEMO_PHOTO,
                photos=[DEMO_PHOTO],
                description="TEST LISTING - not a real property. Used to test the Discovery -> Request to Host -> Open House Manager flow.",
                list_agent_full_name=f"{peter.first_name} {peter.last_name}",
                list_agent_email=peter.email,
                list_office_name=peter.brokerage,
                list_agent_mls_id=peter.mls_id,
                is_manual=True,
                mls_list_date=now,
                days_on_market=0,
            )
            db.add(listing)
            print(f"Created listing {LISTING_KEY}: {listing.unparsed_address}, ${listing.price:,.0f}, {listing.living_area:.0f} sqft")
        else:
            listing.mls_list_date = now
            listing.days_on_market = 0
            listing.home_status = "ACTIVE-BRIGHT"
            listing.price = 2000000
            listing.living_area = 5000
            listing.img_src = DEMO_PHOTO
            listing.photos = [DEMO_PHOTO]
            print(f"Listing {LISTING_KEY} already existed - refreshed so it shows as New again")

        # 3. Point John's Discovery preferences at Villanova so the listing actually surfaces.
        result = await db.execute(select(DiscoveryPreferences).where(DiscoveryPreferences.user_id == john.id))
        prefs = result.scalar_one_or_none()
        if not prefs:
            prefs = DiscoveryPreferences(user_id=john.id)
            db.add(prefs)
        prefs.landmark_address = "Villanova, PA"
        prefs.latitude = VILLANOVA_LAT
        prefs.longitude = VILLANOVA_LON
        prefs.miles = 15
        prefs.state = "PA"
        prefs.min_bedrooms = None
        prefs.min_bathrooms = None
        prefs.min_square_feet = None
        prefs.min_price = None
        prefs.brokerages = None
        prefs.cities = None
        prefs.school_districts = None
        prefs.excluded_agents = None
        print(f"Set {john.first_name}'s Discovery preferences to Villanova, PA / 15mi radius")

        await db.commit()
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
