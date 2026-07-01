"""Upload bug-report attachments to the R2 ``bug-reports/`` prefix.

The server holds the R2 creds; the client never does. Objects are served
publicly at ``https://cdn.slabgorb.com/<key>`` (the same CDN the pack assets
use), so the returned URL embeds directly in a GitHub issue. Because the object
physically lives in R2, the CDN URL is correct even when the UI runs in local
asset mode — so ``cdn_base`` ignores the ``local``/empty override.
"""
from __future__ import annotations

import os
import re

BUCKET = "sidequest"
BUG_REPORT_PREFIX = "bug-reports"
_DEFAULT_CDN = "https://cdn.slabgorb.com"


class R2UploadError(RuntimeError):
    """R2 put_object failed — a hard-dependency failure; the endpoint maps it to 502."""


def _build_client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_S3_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def cdn_base() -> str:
    base = os.environ.get("SIDEQUEST_ASSET_BASE_URL", _DEFAULT_CDN)
    if base in ("", "local"):
        base = _DEFAULT_CDN
    return base.rstrip("/")


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "").strip("._")
    return (cleaned or "file")[:80]


def object_key(report_id: str, index: int, filename: str) -> str:
    return f"{BUG_REPORT_PREFIX}/{report_id}/{index}-{safe_filename(filename)}"


def upload_bytes(key: str, data: bytes, content_type: str) -> str:
    """Put ``data`` at ``key`` in the R2 bucket; return the public CDN URL.
    Any boto/botocore failure is re-raised as ``R2UploadError`` (loud)."""
    try:
        client = _build_client()
        client.put_object(Bucket=BUCKET, Key=key, Body=data, ContentType=content_type)
    except Exception as exc:  # noqa: BLE001 — translate boto/botocore errors to a typed, loud failure
        raise R2UploadError(str(exc)) from exc
    return f"{cdn_base()}/{key}"
