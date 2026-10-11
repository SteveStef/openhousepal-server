import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asyncio
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.database import AgentForm

# Casing/punctuation tweak to the 'selling' and 'home_value' timeline question options
# ("Right Away" -> "Right away", "Yes Right Away" -> "Yes, right away", "Not Interested" ->
# "Not interested"). New forms get this from DEFAULT_FORMS automatically, but forms an agent
# already created before this change still carry the old option text in their saved `questions`
# JSON - this backfills those in place, matching on the exact old option text only.
UPDATES = {
    "selling": {
        "question_id": "timeline",
        "old_options": ["Right Away", "1–3 months", "3–6 months", "6–12+ months", "Flexible"],
        "new_options": ["Right away", "1–3 months", "3–6 months", "6–12+ months", "Flexible"],
    },
    "home_value": {
        "question_id": "sell_timeline",
        "old_options": ["Yes Right Away", "1–3 months", "3–6 months", "6–12+ months", "Not Interested", "Just curious of current value"],
        "new_options": ["Yes, right away", "1–3 months", "3–6 months", "6–12+ months", "Not interested", "Just curious of current value"],
    },
}


async def backfill_timeline_option_casing():
    print("Connecting to database...")

    async with AsyncSessionLocal() as session:
        try:
            stmt = select(AgentForm).where(AgentForm.form_type.in_(UPDATES.keys()))
            result = await session.execute(stmt)
            forms = result.scalars().all()

            updated_count = 0
            for form in forms:
                update = UPDATES[form.form_type]
                questions = list(form.questions or [])
                changed = False
                next_questions = []
                for question in questions:
                    if (
                        question.get("id") == update["question_id"]
                        and question.get("options") == update["old_options"]
                    ):
                        question = {**question, "options": update["new_options"]}
                        changed = True
                    next_questions.append(question)

                if changed:
                    form.questions = next_questions
                    updated_count += 1

            if updated_count:
                print(f"Updating {updated_count} form(s)...")
                await session.commit()
                print(f"Updated {updated_count} form(s) with the new option casing.")
            else:
                print("No forms needed updating.")

        except Exception as e:
            await session.rollback()
            print(f"Error during backfill: {e}")
            raise


if __name__ == "__main__":
    try:
        asyncio.run(backfill_timeline_option_casing())
    except Exception as e:
        print(f"An error occurred: {e}")
