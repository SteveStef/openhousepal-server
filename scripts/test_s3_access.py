"""One-off check that the AWS_* credentials in .env can actually read/write the S3 bucket.

Run from backend/: .venv/Scripts/python.exe scripts/test_s3_access.py
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

BUCKET = os.environ.get("AWS_S3_BUCKET", "openhousepal-images")
REGION = os.environ.get("AWS_REGION", "us-east-1")


def main():
    access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not access_key or not secret_key:
        print("FAIL: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY not set in environment")
        sys.exit(1)

    s3 = boto3.client("s3", region_name=REGION)
    key = f"_healthchecks/{uuid.uuid4().hex}.txt"
    body = b"openhousepal s3 access check"

    try:
        print(f"Bucket: {BUCKET}  Region: {REGION}")

        s3.head_bucket(Bucket=BUCKET)
        print("OK   head_bucket - bucket exists and is reachable")

        s3.put_object(Bucket=BUCKET, Key=key, Body=body, ContentType="text/plain")
        print(f"OK   put_object  - wrote {key}")

        got = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        assert got == body, "downloaded content did not match what was uploaded"
        print("OK   get_object  - read back matching content")

        listing = s3.list_objects_v2(Bucket=BUCKET, Prefix="_healthchecks/", MaxKeys=5)
        print(f"OK   list_objects_v2 - {listing.get('KeyCount', 0)} object(s) under _healthchecks/")

        s3.delete_object(Bucket=BUCKET, Key=key)
        print(f"OK   delete_object - removed {key}")

        try:
            acl = s3.get_bucket_acl(Bucket=BUCKET)
            grants = [g["Permission"] for g in acl.get("Grants", [])]
            print(f"INFO bucket ACL grants visible to this key: {grants}")
        except ClientError as e:
            print(f"INFO get_bucket_acl not permitted (not required): {e.response['Error']['Code']}")

        print("\nAll checks passed. This IAM key can read and write objects in the bucket.")
    except NoCredentialsError:
        print("FAIL: boto3 could not find credentials")
        sys.exit(1)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        msg = e.response.get("Error", {}).get("Message")
        print(f"FAIL: {code} - {msg}")
        sys.exit(1)


if __name__ == "__main__":
    main()
