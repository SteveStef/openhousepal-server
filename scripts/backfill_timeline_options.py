import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asyncio
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.database import AgentForm

# The 'selling' and 'home_value' default templates' timeline question wording/options changed
# (ASAP -> Right Away, "Just curious"/"Not sure yet" reworked). New forms get this from
# DEFAULT_FORMS automatically, but forms an agent already created before this change still carry
# the old label/options in their saved `questions` JSON - this backfills those in place. Only
# replaces the question if it still exactly matches the OLD default (label + options), so an
# agent's own edits to this question are left untouched.
UPDATES = {
    "selling": {
        "question_id": "timeline",
        "old_label": "When are you hoping to sell?",
        "old_options": ["ASAP", "1–3 months", "3–6 months", "6–12+ months", "Just curious"],
        "new_label": "When are you hoping to sell?",
        "new_options": ["Right Away", "1–3 months", "3–6 months", "6–12+ months", "Flexible"],
    },
    "home_value": {
        "question_id": "sell_timeline",
        "old_label": "When are you thinking of selling?",
        "old_options": ["ASAP", "1–3 months", "3–6 months", "6–12+ months", "Not sure yet"],
        "new_label": "Are you thinking of selling?",
        "new_options": ["Yes Right Away", "1–3 months", "3–6 months", "6–12+ months", "Not Interested", "Just curious of current value"],
    },
}


async def backfill_timeline_options():
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
                        and question.get("label") == update["old_label"]
                        and question.get("options") == update["old_options"]
                    ):
                        question = {**question, "label": update["new_label"], "options": update["new_options"]}
                        changed = True
                    next_questions.append(question)

                if changed:
                    form.questions = next_questions
                    updated_count += 1

            if updated_count:
                print(f"Updating {updated_count} form(s)...")
                await session.commit()
                print(f"Updated {updated_count} form(s) with the new timeline question wording/options.")
            else:
                print("No forms needed updating.")

        except Exception as e:
            await session.rollback()
            print(f"Error during backfill: {e}")
            raise


if __name__ == "__main__":
    try:
        asyncio.run(backfill_timeline_options())
    except Exception as e:
        print(f"An error occurred: {e}")
