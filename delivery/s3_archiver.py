# delivery/s3_archiver.py
import datetime
import logging
import os
import uuid

from utils.aws_factory import get_client

logger = logging.getLogger(__name__)

_s3_client = None


def _get_s3_client():
    """Lazy initialization of S3 client (delegates to shared factory)."""
    global _s3_client
    if _s3_client is None:
        _s3_client = get_client("s3")
    return _s3_client


def reset_s3_client():
    global _s3_client
    _s3_client = None


def archive_report(report):
    """Archive report to S3. Returns the object key."""
    bucket = os.environ.get("REPORT_BUCKET")
    if not bucket:
        logger.error("REPORT_BUCKET environment variable is not set")
        raise ValueError("REPORT_BUCKET is not set")
    if bucket.startswith("arn:") or "/" in bucket:
        raise ValueError("REPORT_BUCKET must be a bucket name, not an ARN or path")

    if not isinstance(report, str) or not report:
        raise ValueError("report must be a non-empty string")

    s3 = _get_s3_client()
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    prefix = os.environ.get("REPORT_PREFIX", "reports").strip("/") or "reports"
    request_id = os.environ.get("AWS_REQUEST_ID", "") or uuid.uuid4().hex[:8]
    key = f"{prefix}/{timestamp}-{request_id}.md"

    sse = os.environ.get("S3_SSE", "AES256")
    kms_key = os.environ.get("S3_KMS_KEY_ID", "")
    put_kwargs = {
        "Bucket": bucket,
        "Key": key,
        "Body": report.encode("utf-8"),
        "ContentType": "text/markdown",
    }
    if kms_key:
        put_kwargs["ServerSideEncryption"] = "aws:kms"
        put_kwargs["SSEKMSKeyId"] = kms_key
    elif sse:
        put_kwargs["ServerSideEncryption"] = sse
    storage_class = os.environ.get("S3_STORAGE_CLASS", "")
    if storage_class:
        put_kwargs["StorageClass"] = storage_class

    logger.info(f"Archiving report to s3://{bucket}/{key}")
    try:
        s3.put_object(**put_kwargs)
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "") if hasattr(e, "response") else ""
        logger.error(f"S3 archive failed ({code or type(e).__name__}): {e}", exc_info=True)
        raise
    logger.info("Report archived successfully to S3")
    return key
