import uuid
import os
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.database import User, Collection, Property, OpenHouseEvent, CollectionPreferences, OpenHouseVisitor

# Get the admin email to attach data to
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@openhousepal.com")

async def seed_dummy_data():
    """
    Populate the database with dummy data for the admin user if it doesn't exist.
    """
    print("Checking if dummy data needs to be seeded...")
    async with AsyncSessionLocal() as session:
        # 1. Get the user (Admin)
        result = await session.execute(select(User).where(User.email == ADMIN_EMAIL))
        user = result.scalars().first()
        
        if not user:
            print(f"Skipping seed: Admin user {ADMIN_EMAIL} not found.")
            return

        # Check if we already have our specific dummy property to avoid re-seeding
        # We'll check for zpid 10001
        existing_check = await session.execute(select(Property).where(Property.zpid == 10001))
        if existing_check.scalars().first():
            print("Dummy data already exists. Skipping seed.")
            return

        print(f"Seeding dummy data for user: {user.email}")

        # 2. Create Dummy Properties
        dummy_properties_data = [
            {
                "zpid": 10001,
                "street_address": "123 Maple Street",
                "city": "Springfield",
                "state": "IL",
                "zipcode": "62704",
                "price": 350000,
                "bedrooms": 3,
                "bathrooms": 2.0,
                "living_area": 1800,
                "lot_size": 6000,
                "home_type": "SINGLE_FAMILY",
                "home_status": "FOR_SALE",
                "latitude": 39.7817,
                "longitude": -89.6501,
                "img_src": "https://img.freepik.com/free-vector/charming-house-with-tree-illustration_1308-176337.jpg?semt=ais_hybrid&w=740&q=80"
            },
            {
                "zpid": 10002,
                "street_address": "456 Oak Avenue",
                "city": "Shelbyville",
                "state": "IL",
                "zipcode": "62565",
                "price": 420000,
                "bedrooms": 4,
                "bathrooms": 3.0,
                "living_area": 2400,
                "lot_size": 8000,
                "home_type": "SINGLE_FAMILY",
                "home_status": "FOR_SALE",
                "latitude": 39.4089,
                "longitude": -88.7995,
                "img_src": "https://img.freepik.com/free-vector/charming-house-with-tree-illustration_1308-176337.jpg?semt=ais_hybrid&w=740&q=80"
            },
            {
                "zpid": 10003,
                "street_address": "789 Pine Lane",
                "city": "Capital City",
                "state": "IL",
                "zipcode": "62701",
                "price": 280000,
                "bedrooms": 2,
                "bathrooms": 1.5,
                "living_area": 1200,
                "lot_size": 4000,
                "home_type": "CONDO",
                "home_status": "FOR_SALE",
                "latitude": 39.7990,
                "longitude": -89.6430,
                "img_src": "https://img.freepik.com/free-vector/charming-house-with-tree-illustration_1308-176337.jpg?semt=ais_hybrid&w=740&q=80"
            }
        ]

        properties = []
        for prop_data in dummy_properties_data:
            # Re-check just in case (though we checked the first one)
            existing = await session.execute(select(Property).where(Property.zpid == prop_data["zpid"]))
            existing_prop = existing.scalars().first()
            
            if existing_prop:
                properties.append(existing_prop)
            else:
                new_prop = Property(**prop_data)
                session.add(new_prop)
                properties.append(new_prop)
        
        await session.flush()

        # 3. Create Collections
        collection_names = ["My Favorites", "Potential Investments", "Dream Homes"]
        
        for name in collection_names:
            new_collection = Collection(
                name=name,
                description=f"A dummy collection of {name}",
                owner_id=user.id,
                share_token=str(uuid.uuid4()),
                is_public=True
            )
            
            # Add properties to collection
            new_collection.properties.extend(properties)
            
            session.add(new_collection)
            await session.flush() # Flush to get ID for preferences

            # Create Preferences
            prefs = CollectionPreferences(
                collection_id=new_collection.id,
                min_beds=2,
                max_price=500000,
                cities=["Springfield", "Shelbyville"]
            )
            session.add(prefs)
            
            print(f"Created collection: {name}")

        # 4. Create Open House Event ("PDF")
        open_house = OpenHouseEvent(
            qr_code=str(uuid.uuid4()),
            agent_id=user.id,
            address="123 Maple Street",
            abbreviated_address="123 Maple",
            house_type="SINGLE_FAMILY",
            price=350000,
            bedrooms=3,
            bathrooms=2.0,
            living_area=1800,
            city="Springfield",
            state="IL",
            zipcode="62704",
            cover_image_url="https://img.freepik.com/free-vector/charming-house-with-tree-illustration_1308-176337.jpg?semt=ais_hybrid&w=740&q=80"
        )
        session.add(open_house)
        await session.flush()
        print("Created Open House Event.")

        # 5. Create Open House Visitors
        visitors_data = [
            {
                "full_name": "John Doe",
                "email": "john.doe@example.com",
                "phone": "555-0101",
                "has_agent": "NO",
                "interested_in_similar": True
            },
            {
                "full_name": "Jane Smith",
                "email": "jane.smith@example.com",
                "phone": "555-0102",
                "has_agent": "YES",
                "interested_in_similar": False
            },
            {
                "full_name": "Bob Wilson",
                "email": "bob.wilson@example.com",
                "phone": "555-0103",
                "has_agent": "LOOKING",
                "interested_in_similar": True
            }
        ]

        for v_data in visitors_data:
            visitor = OpenHouseVisitor(
                open_house_event_id=open_house.id,
                qr_code=open_house.qr_code,
                **v_data
            )
            session.add(visitor)
        
        print(f"Created {len(visitors_data)} Open House Visitors.")

        await session.commit()
        print("Database population complete.")
