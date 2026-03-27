import asyncio
import json
import os
import sys
from datetime import datetime
from decimal import Decimal

# Add the current directory to sys.path to allow importing from 'app'
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy.future import select
from app.database import AsyncSessionLocal
from app.models.database import Property

class CustomEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)

async def query_property():
    property_id = "0bea12bf-7109-48a7-8807-ae8cf5c9c3c5"
    print(f"Querying property with ID: {property_id}")
    
    async with AsyncSessionLocal() as session:
        try:
            result = await session.execute(select(Property).where(Property.id == property_id))
            prop = result.scalar_one_or_none()
            
            if prop:
                # Convert SQLAlchemy model to dict
                prop_dict = {}
                for column in prop.__table__.columns:
                    value = getattr(prop, column.name)
                    prop_dict[column.name] = value
                
                output_file = "tmp.json"
                with open(output_file, "w") as f:
                    json.dump(prop_dict, f, indent=4, cls=CustomEncoder)
                print(f"Successfully saved property to {output_file}")
            else:
                print(f"Property with ID {property_id} not found in database.")
        except Exception as e:
            print(f"An error occurred: {e}")

if __name__ == "__main__":
    asyncio.run(query_property())
