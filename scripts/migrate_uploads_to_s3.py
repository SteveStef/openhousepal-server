"""One-off migration: uploads every file under backend/uploads/ to S3, rewrites every DB
reference (users.photo_url, studio_brand_kits.*_logo_url, blogs.cover_image_url, and the
freeform agent_hubs.config JSONB blob, which stores sale-photo/profile-photo URLs both as
bare relative paths and as absolute http://localhost:8080/uploads/... links), then deletes
the local files. Safe to re-run - rows/files already migrated are simply skipped.

Run from backend/: .venv/Scripts/python.exe scripts/migrate_uploads_to_s3.py
"""
import asyncio
import json
import mimetypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text

from app.database import AsyncSessionLocal
from app.services import s3_service

UPLOAD_ROOT = "uploads"
LOCAL_HOST_PREFIXES = ["http://localhost:8080", "http://127.0.0.1:8080"]


def replace_in_json(value, old_to_new: dict):
    if isinstance(value, str):
        return old_to_new.get(value, value)
    if isinstance(value, dict):
        return {k: replace_in_json(v, old_to_new) for k, v in value.items()}
    if isinstance(value, list):
        return [replace_in_json(v, old_to_new) for v in value]
    return value


async def main():
    # 1. Upload every local file to S3, building old-path -> new-url maps (relative + absolute forms).
    old_to_new: dict[str, str] = {}
    for subdir in ("agent-photos", "agent-sale-photos", "studio-media"):
        dir_path = os.path.join(UPLOAD_ROOT, subdir)
        if not os.path.isdir(dir_path):
            continue
        for filename in os.listdir(dir_path):
            local_path = os.path.join(dir_path, filename)
            ext = os.path.splitext(filename)[1]
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            with open(local_path, "rb") as f:
                data = f.read()
            new_url = s3_service.upload_bytes(data, subdir, ext, content_type)
            relative = f"/uploads/{subdir}/{filename}"
            old_to_new[relative] = new_url
            for host in LOCAL_HOST_PREFIXES:
                old_to_new[f"{host}{relative}"] = new_url
            print(f"uploaded {relative} -> {new_url}")

    # 2. Simple string columns.
    async with AsyncSessionLocal() as db:
        updated = 0
        for old, new in old_to_new.items():
            if old.startswith("http://"):
                continue  # only bare relative paths are stored in the simple columns
            r = await db.execute(text("update users set photo_url = :new where photo_url = :old"), {"new": new, "old": old})
            updated += r.rowcount
            r = await db.execute(
                text(
                    "update studio_brand_kits set "
                    "personal_logo_url = case when personal_logo_url = :old then :new else personal_logo_url end, "
                    "team_logo_url = case when team_logo_url = :old then :new else team_logo_url end, "
                    "brokerage_logo_url = case when brokerage_logo_url = :old then :new else brokerage_logo_url end "
                    "where personal_logo_url = :old or team_logo_url = :old or brokerage_logo_url = :old"
                ),
                {"new": new, "old": old},
            )
            updated += r.rowcount
            r = await db.execute(
                text(
                    "update blogs set "
                    "cover_image_url = case when cover_image_url = :old then :new else cover_image_url end, "
                    "social_image_url = case when social_image_url = :old then :new else social_image_url end "
                    "where cover_image_url = :old or social_image_url = :old"
                ),
                {"new": new, "old": old},
            )
            updated += r.rowcount
        await db.commit()
        print(f"simple-column rows updated: {updated}")

        # 3. agent_hubs.config JSONB - walk and replace any matching string (relative or absolute).
        r = await db.execute(text("select id, config from agent_hubs"))
        hub_rows = r.fetchall()
        hub_updates = 0
        for hub_id, config in hub_rows:
            new_config = replace_in_json(config, old_to_new)
            if new_config != config:
                await db.execute(
                    text("update agent_hubs set config = cast(:config as jsonb) where id = :id"),
                    {"config": json.dumps(new_config), "id": hub_id},
                )
                hub_updates += 1
        await db.commit()
        print(f"agent_hubs.config rows updated: {hub_updates}")

    # 4. Verify nothing local remains referenced.
    async with AsyncSessionLocal() as db:
        r = await db.execute(text("select count(*) from users where photo_url like '/uploads/%'"))
        remaining = r.scalar()
        r = await db.execute(text("select count(*) from agent_hubs where config::text like '%/uploads/%'"))
        remaining += r.scalar()
        r = await db.execute(text("select count(*) from blogs where cover_image_url like '/uploads/%' or social_image_url like '/uploads/%'"))
        remaining += r.scalar()
        r = await db.execute(text("select count(*) from studio_brand_kits where personal_logo_url like '/uploads/%' or team_logo_url like '/uploads/%' or brokerage_logo_url like '/uploads/%'"))
        remaining += r.scalar()
        print(f"remaining /uploads/ references in DB: {remaining}")
        if remaining:
            print("ABORTING local file cleanup - some references were not rewritten, investigate before deleting.")
            return

    # 5. Delete local files now that nothing references them.
    for subdir in ("agent-photos", "agent-sale-photos", "studio-media"):
        dir_path = os.path.join(UPLOAD_ROOT, subdir)
        if not os.path.isdir(dir_path):
            continue
        for filename in os.listdir(dir_path):
            os.remove(os.path.join(dir_path, filename))
        os.rmdir(dir_path)
        print(f"removed local dir {dir_path}")
    if os.path.isdir(UPLOAD_ROOT) and not os.listdir(UPLOAD_ROOT):
        os.rmdir(UPLOAD_ROOT)
        print(f"removed {UPLOAD_ROOT}")


if __name__ == "__main__":
    asyncio.run(main())
