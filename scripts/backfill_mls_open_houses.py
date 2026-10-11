"""
One-time backfill for Property.mls_open_house_* fields.

The regular open house sync (property_sync_service.run_open_house_sync, scheduled every 30
minutes in main.py) only picks up Bright open house records *modified after* it starts tracking
a checkpoint. Anything already scheduled before that point needs this instead - a full pull of
every currently-upcoming open house, applied once (or re-run occasionally as a sanity refresh).

Usage:
    python scripts/backfill_mls_open_houses.py [--max-pages N]

Hits the live Bright MLS API - run deliberately, not as part of any automated pipeline.
"""
import argparse
import asyncio
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

from dotenv import load_dotenv
load_dotenv(root_dir / ".env")

from app.services.property_sync_service import PropertySyncService
from app.services.bright_mls_service import bright_mls_service
from app.config.logging import get_logger

logger = get_logger(__name__)


async def main(max_pages: int | None):
    service = PropertySyncService()
    try:
        stats = await service.run_open_house_backfill(max_pages=max_pages)
        logger.info(f"Backfill finished: {stats}")
    finally:
        await bright_mls_service.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-pages", type=int, default=None, help="Cap the number of 200-record pages pulled")
    args = parser.parse_args()
    asyncio.run(main(args.max_pages))
