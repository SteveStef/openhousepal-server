import boto3
import os
import sys
from dotenv import load_dotenv

load_dotenv()

S3_BUCKET = os.getenv("S3_BACKUP_BUCKET")
DB_PATH = "./collections.db"

def list_backups():
    s3 = boto3.client('s3')
    response = s3.list_objects_v2(Bucket=S3_BUCKET)

    if 'Contents' not in response:
        print("No backups found")
        return []

    backups = [obj for obj in response['Contents'] if obj['Key'].startswith('collections-') and obj['Key'].endswith('.db')]
    backups.sort(key=lambda x: x['LastModified'], reverse=True)

    return backups

def restore_backup(backup_key=None):
    try:
        s3 = boto3.client('s3')
        backups = list_backups()

        if not backups:
            sys.exit(1)

        # Use latest backup if none specified
        if not backup_key:
            backup_key = backups[0]['Key']
            print(f"Restoring latest backup: {backup_key}")
        else:
            print(f"Restoring backup: {backup_key}")

        # Download from S3
        temp_file = f"/tmp/{backup_key}"
        s3.download_file(S3_BUCKET, backup_key, temp_file)

        # Backup current database if it exists
        if os.path.exists(DB_PATH):
            backup_current = f"{DB_PATH}.pre-restore"
            os.rename(DB_PATH, backup_current)
            print(f"✓ Current database backed up to: {backup_current}")

        # Restore the backup
        os.rename(temp_file, DB_PATH)
        print(f"✓ Database restored from: {backup_key}")

    except Exception as e:
        print(f"✗ Restore failed: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Restore specific backup
        restore_backup(sys.argv[1])
    else:
        # List backups and restore latest
        backups = list_backups()
        if backups:
            print("\nAvailable backups:")
            for i, backup in enumerate(backups):
                print(f"{i+1}. {backup['Key']} ({backup['LastModified']})")
            print()
        restore_backup()
