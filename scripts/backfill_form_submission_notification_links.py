import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asyncio
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.database import Notification

# FORM_SUBMISSION notifications used to link to "/agent-hub?tab=Forms Results&formId=...".
# /agent-hub is just a redirect stub to /studio/site that drops query params, and even if it
# didn't, "Forms Results" isn't a tab value the page understands (it expects tab=results on the
# /studio/forms route) - so clicking these notifications never landed the agent on the submission.
# This rewrites existing rows in place to the working link.
OLD_PREFIX = "/agent-hub?tab=Forms Results&formId="
NEW_PREFIX = "/studio/forms?tab=results&formId="


async def backfill_form_submission_notification_links():
    print("Connecting to database...")

    async with AsyncSessionLocal() as session:
        try:
            stmt = select(Notification).where(Notification.type == "FORM_SUBMISSION")
            result = await session.execute(stmt)
            notifications = result.scalars().all()

            updated_count = 0
            for notification in notifications:
                if notification.link and notification.link.startswith(OLD_PREFIX):
                    form_id = notification.link[len(OLD_PREFIX):]
                    notification.link = f"{NEW_PREFIX}{form_id}"
                    updated_count += 1

            if updated_count:
                print(f"Updating {updated_count} notification(s)...")
                await session.commit()
                print(f"Updated {updated_count} notification(s) with the corrected link.")
            else:
                print("No notifications needed updating.")

        except Exception as e:
            await session.rollback()
            print(f"Error during backfill: {e}")
            raise


if __name__ == "__main__":
    try:
        asyncio.run(backfill_form_submission_notification_links())
    except Exception as e:
        print(f"An error occurred: {e}")
