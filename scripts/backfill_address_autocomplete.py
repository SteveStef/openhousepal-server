import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asyncio
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.database import AgentForm

# The 'selling' and 'home_value' default templates' property_address question was upgraded from
# a plain short_text input to the Google Places autocomplete type. New forms get this from
# DEFAULT_FORMS automatically, but forms an agent already created before this change still carry
# the old short_text type in their saved `questions` JSON - this backfills those in place.
TARGET_FORM_TYPES = {"selling", "home_value"}


async def backfill_address_autocomplete():
    print("Connecting to database...")

    async with AsyncSessionLocal() as session:
        try:
            stmt = select(AgentForm).where(AgentForm.form_type.in_(TARGET_FORM_TYPES))
            result = await session.execute(stmt)
            forms = result.scalars().all()

            updated_count = 0
            for form in forms:
                questions = list(form.questions or [])
                changed = False
                next_questions = []
                for question in questions:
                    if question.get("id") == "property_address" and question.get("type") == "short_text":
                        question = {**question, "type": "address_autocomplete"}
                        changed = True
                    next_questions.append(question)

                if changed:
                    form.questions = next_questions
                    updated_count += 1

            if updated_count:
                print(f"Updating {updated_count} form(s)...")
                await session.commit()
                print(f"Updated {updated_count} form(s) to use address_autocomplete.")
            else:
                print("No forms needed updating.")

        except Exception as e:
            await session.rollback()
            print(f"Error during backfill: {e}")
            raise


if __name__ == "__main__":
    try:
        asyncio.run(backfill_address_autocomplete())
    except Exception as e:
        print(f"An error occurred: {e}")
