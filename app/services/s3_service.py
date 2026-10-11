import logging
import os
import uuid
from functools import lru_cache
from typing import Optional
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AWS_S3_BUCKET = os.environ.get("AWS_S3_BUCKET", "openhousepal-images")
# The bucket's own direct endpoint - never used to build NEW urls (see PUBLIC_URL_HOST below),
# only kept so delete_by_url can still recognize + delete objects whose stored URL predates the
# CDN (not yet migrated, or CDN_HOST unset in this environment).
AWS_S3_HOST = f"{AWS_S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com"
# CloudFront distribution in front of the bucket (2026-09-04, distribution id E3KFQ9YMYE6SML,
# domain dk5bvrzdygq8w.cloudfront.net - default *.cloudfront.net domain, no custom domain/ACM
# cert needed) - every photo/video is public and unauthenticated either way, so this is a pure
# cache in front of the same objects, not an access-control change. Falls back to the bucket's
# own direct endpoint if CDN_HOST isn't set (e.g. an environment without it configured yet) so
# nothing breaks without it. Set CDN_HOST in production's environment the same way.
PUBLIC_URL_HOST = os.environ.get("CDN_HOST") or AWS_S3_HOST


@lru_cache(maxsize=1)
def _client():
    return boto3.client("s3", region_name=AWS_REGION)


def upload_bytes(data: bytes, key_prefix: str, extension: str, content_type: str, cache_control: str | None = None) -> str:
    """Uploads bytes to the public images bucket under `key_prefix/<uuid><extension>` and
    returns the object's public HTTPS URL. Raises HTTPException-friendly ClientError on failure.
    `cache_control` lets the CDN and browsers keep a never-changing file (every key is a fresh
    uuid) instead of re-fetching it."""
    key = f"{key_prefix.strip('/')}/{uuid.uuid4()}{extension}"
    extra = {"CacheControl": cache_control} if cache_control else {}
    _client().put_object(Bucket=AWS_S3_BUCKET, Key=key, Body=data, ContentType=content_type, **extra)
    return f"https://{PUBLIC_URL_HOST}/{key}"


def delete_by_url(url: Optional[str]) -> None:
    """Deletes the S3 object referenced by a public URL previously returned from upload_bytes.
    No-ops for None/empty or any URL that isn't one of our S3 objects (e.g. legacy /uploads/
    local paths, or external MLS URLs) so it's safe to call on any stored photo_url. Accepts
    either the CDN host (PUBLIC_URL_HOST, what new URLs use) or the bucket's own direct host
    (AWS_S3_HOST) - a delete always goes straight to S3 either way (CloudFront doesn't proxy
    DELETE), this just recognizes a not-yet-migrated pre-CDN URl as one of ours too."""
    if not url:
        return
    parsed = urlparse(url)
    if parsed.netloc not in (PUBLIC_URL_HOST, AWS_S3_HOST):
        return
    key = parsed.path.lstrip("/")
    if not key:
        return
    try:
        _client().delete_object(Bucket=AWS_S3_BUCKET, Key=key)
    except ClientError as e:
        logger.warning("s3 delete_object failed", extra={"key": key, "error": str(e)})
