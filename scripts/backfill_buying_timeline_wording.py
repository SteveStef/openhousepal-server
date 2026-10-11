import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asyncio
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.database import AgentForm

# The 'buying' default template's timeline question changed "ASAP" -> "Right away". New forms get
# this from DEFAULT_FORMS automatically, but forms an agent already created before this change
# still carry the old option text in their saved `questions` JSON - this backfills those in place,
# matching on the exact old option list only.
QUESTION_ID = "timeline"
OLD_OPTIONS = ["ASAP", "1–3 months", "3–6 months", "6–12 months", "Just researching"]
NEW_OPTIONS = ["Right away", "1–3 months", "3–6 months", "6–12 months", "Just researching"]


async def backfill_buying_timeline_wording():
    print("Connecting to database...")

    async with AsyncSessionLocal() as session:
        try:
            stmt = select(AgentForm).where(AgentForm.form_type == "buying")
            result = await session.execute(stmt)
            forms = result.scalars().all()

            updated_count = 0
            for form in forms:
                questions = list(form.questions or [])
                changed = False
                next_questions = []
                for question in questions:
                    if question.get("id") == QUESTION_ID and question.get("options") == OLD_OPTIONS:
                        question = {**question, "options": NEW_OPTIONS}
                        changed = True
                    next_questions.append(question)

                if changed:
                    form.questions = next_questions
                    updated_count += 1

            if updated_count:
                print(f"Updating {updated_count} form(s)...")
                await session.commit()
                print(f"Updated {updated_count} form(s) with the new timeline wording.")
            else:
                print("No forms needed updating.")

        except Exception as e:
            await session.rollback()
            print(f"Error during backfill: {e}")
            raise


if __name__ == "__main__":
    try:
        asyncio.run(backfill_buying_timeline_wording())
    except Exception as e:
        print(f"An error occurred: {e}")
