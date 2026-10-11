import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asyncio
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.database import AgentForm

# The 'home_value' default template's description changed wording. New forms get this from
# DEFAULT_FORMS automatically, but forms an agent already created before this change still carry
# the old description - this backfills those in place, only when it still exactly matches the old
# default (so an agent's own custom description is left untouched).
OLD_DESCRIPTION = "Collect a homeowner's property information so you can prepare a home-value estimate."
NEW_DESCRIPTION = "Collect a potential sellers information to create a home-value estimate."


async def backfill_home_value_description():
    print("Connecting to database...")

    async with AsyncSessionLocal() as session:
        try:
            stmt = select(AgentForm).where(
                AgentForm.form_type == "home_value",
                AgentForm.description == OLD_DESCRIPTION,
            )
            result = await session.execute(stmt)
            forms = result.scalars().all()

            for form in forms:
                form.description = NEW_DESCRIPTION

            if forms:
                print(f"Updating {len(forms)} form(s)...")
                await session.commit()
                print(f"Updated {len(forms)} form(s) with the new description.")
            else:
                print("No forms needed updating.")

        except Exception as e:
            await session.rollback()
            print(f"Error during backfill: {e}")
            raise


if __name__ == "__main__":
    try:
        asyncio.run(backfill_home_value_description())
    except Exception as e:
        print(f"An error occurred: {e}")
