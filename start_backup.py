import boto3
import sqlite3
import os
from datetime import datetime
import sys

from dotenv import load_dotenv
load_dotenv()

S3_BUCKET = os.getenv("S3_BACKUP_BUCKET")
DB_PATH = "./collections.db"

def backup_to_s3():
    try:
        date = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_file = f"/tmp/collections-{date}.db"

        # Create backup using Python's sqlite3 module
        source = sqlite3.connect(DB_PATH)
        dest = sqlite3.connect(backup_file)
        source.backup(dest)
        dest.close()
        source.close()

        s3 = boto3.client('s3')
        s3.upload_file(
            backup_file,
            S3_BUCKET,
            f"collections-{date}.db"
        )

        print(f"✓ Backup uploaded: collections-{date}.db")

        # Keep only the 5 most recent backups
        response = s3.list_objects_v2(Bucket=S3_BUCKET)
        if 'Contents' in response:
            backups = [obj for obj in response['Contents'] if obj['Key'].startswith('collections-') and obj['Key'].endswith('.db')]
            backups.sort(key=lambda x: x['LastModified'])

            if len(backups) > 5:
                to_delete = backups[:-5]
                for backup in to_delete:
                    s3.delete_object(Bucket=S3_BUCKET, Key=backup['Key'])
                    print(f"✓ Deleted old backup: {backup['Key']}")

        os.remove(backup_file)

    except Exception as e:
        print(f"✗ Backup failed: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    backup_to_s3()
