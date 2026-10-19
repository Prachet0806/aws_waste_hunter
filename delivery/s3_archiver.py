# delivery/s3_archiver.py
import datetime
import hashlib
import logging
import os
import time
import uuid

from utils.aws_factory import get_client
from utils.scan_context import CURRENT

logger = logging.getLogger(__name__)

_s3_client = None
S3_CONFLICT_MAX_ATTEMPTS = 3


def _get_s3_client():
    """Lazy initialization of S3 client (delegates to shared factory)."""
    if CURRENT.get():
        return get_client("s3")
    global _s3_client
    if _s3_client is None:
        _s3_client = get_client("s3")
    return _s3_client


def reset_s3_client():
    global _s3_client
    _s3_client = None


def _put_once(s3, bucket, key, body, content_type, extra=None):
    """Create deterministic artifacts once, and verify the winner on retries."""
    metadata = {"waste-hunter-sha256": hashlib.sha256(body).hexdigest()}
    params = {"Bucket": bucket, "Key": key, "Body": body, "ContentType": content_type,
              "Metadata": metadata, "IfNoneMatch": "*", **(extra or {})}
    for attempt in range(S3_CONFLICT_MAX_ATTEMPTS):
        try:
            s3.put_object(**params)
            return key
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code not in {"PreconditionFailed", "412", "ConditionalRequestConflict"}:
                raise
            try:
                existing = s3.head_object(Bucket=bucket, Key=key)
            except Exception as head_error:
                head_code = getattr(head_error, "response", {}).get("Error", {}).get("Code", "")
                object_not_visible = head_code in {"404", "NoSuchKey", "NotFound"}
                can_retry_race = code == "ConditionalRequestConflict" and object_not_visible
                if not can_retry_race or attempt + 1 == S3_CONFLICT_MAX_ATTEMPTS:
                    raise exc from head_error
                time.sleep(min(0.05 * (2 ** attempt), 0.2))
                continue
            if existing.get("Metadata", {}).get("waste-hunter-sha256") != metadata["waste-hunter-sha256"]:
                raise ValueError("Idempotent S3 key already contains different content") from exc
            return key
    raise RuntimeError("S3 conditional write attempts exhausted")


def archive_report(report, key=None):
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
    key = key or f"{prefix}/{timestamp}-{request_id}.md"

    sse = os.environ.get("S3_SSE", "AES256")
    kms_key = os.environ.get("S3_KMS_KEY_ID", "")
    body = report.encode("utf-8")
    extra = {}
    if kms_key:
        extra["ServerSideEncryption"] = "aws:kms"
        extra["SSEKMSKeyId"] = kms_key
    elif sse:
        extra["ServerSideEncryption"] = sse
    storage_class = os.environ.get("S3_STORAGE_CLASS", "")
    if storage_class:
        extra["StorageClass"] = storage_class

    logger.info(f"Archiving report to s3://{bucket}/{key}")
    try:
        _put_once(s3, bucket, key, body, "text/markdown", extra)
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "") if hasattr(e, "response") else ""
        logger.error(f"S3 archive failed ({code or type(e).__name__}): {e}", exc_info=True)
        raise
    logger.info("Report archived successfully to S3")
    return key


def archive_json(value, key, *, on_encoded=None):
    import json
    bucket = os.environ['REPORT_BUCKET']
    body = json.dumps(value, allow_nan=False, default=str).encode()
    if on_encoded is not None:
        on_encoded(len(body))
    extra = {"ServerSideEncryption": os.getenv("S3_SSE", "AES256")}
    if os.getenv("S3_KMS_KEY_ID"):
        extra.update(ServerSideEncryption="aws:kms", SSEKMSKeyId=os.environ['S3_KMS_KEY_ID'])
    return _put_once(_get_s3_client(), bucket, key, body, "application/json", extra)
