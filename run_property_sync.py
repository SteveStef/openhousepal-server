#!/usr/bin/env python3
"""
Simple script to manually run property sync.

Usage:
    python run_property_sync.py
"""

import asyncio
import sys
from pathlib import Path

# Add server directory to Python path
server_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(server_dir))

from app.services.property_sync_service import PropertySyncService
from app.config.logging import configure_logging

# Configure logging
configure_logging()


async def main():
    """Run property sync for all active collections."""
    sync_service = PropertySyncService()
    result = await sync_service.sync_all_active_collections()

    print(f"\nSync completed: {result['collections_processed']} collections, "
          f"{result['total_new_properties']} new properties added\n")

    return 0 if result['success'] else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
