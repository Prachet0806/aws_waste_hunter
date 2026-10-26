# delivery/sns_sender.py
import logging
import math
import os
import re
import uuid

from utils.aws_factory import get_client
from utils.scan_context import CURRENT

logger = logging.getLogger(__name__)

_sns_client = None

SNS_MAX_BYTES = 262144
SNS_MAX_ATTRS = 10


def _get_sns_client():
    """Lazy initialization of SNS client (delegates to shared factory)."""
    if CURRENT.get():
        return get_client("sns", region=_topic_region())
    global _sns_client
    if _sns_client is None:
        region = _topic_region() or None
        _sns_client = get_client("sns", region=region)
    return _sns_client


def reset_sns_client():
    global _sns_client
    _sns_client = None


def _topic_region():
    if os.environ.get("SNS_TOPIC_REGION"):
        return os.environ["SNS_TOPIC_REGION"].strip()
    arn = os.environ.get("SNS_TOPIC_ARN", "")
    m = re.match(r"^arn:aws[a-z-]*:sns:([a-z0-9-]+):", arn or "")
    if m:
        return m.group(1)
    explicit = (
        os.environ.get("SNS_TOPIC_REGION")
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
    )
    return (explicit or "").strip() or None


def _safe_number(value):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num) or abs(num) > 10**9:
        return None
    # SNS Number must be plain decimal without NaN/Inf.
    if num.is_integer():
        return str(int(num))
    return format(num, ".5f").rstrip("0").rstrip(".")


def _build_message_attributes(result):
    """Build SNS message attributes from handler result for filtering."""
    attrs = {}
    result = result or {}

    def add_number(name, value):
        if value is None:
            return
        safe = _safe_number(value)
        if safe is None:
            logger.warning(f"Skipping SNS attribute {name}: non-numeric {value!r}")
            return
        if len(attrs) >= SNS_MAX_ATTRS:
            return
        attrs[name] = {"DataType": "Number", "StringValue": safe}

    def add_string(name, value, max_len=256):
        if value is None:
            return
        text = str(value)[:max_len]
        if len(attrs) >= SNS_MAX_ATTRS:
            return
        attrs[name] = {"DataType": "String", "StringValue": text}

    # Total monthly waste (numeric for filter policies)
    if "monthly_waste" in result:
        add_number("monthly_waste", result["monthly_waste"])

    # Resource count
    if "resources" in result:
        add_number("resource_count", result["resources"])

    # High waste flag
    if result.get("high_waste") is not None:
        add_string("high_waste", str(bool(result["high_waste"])).lower())

    # Compliance percent
    if result.get("compliance_percent") is not None:
        add_number("compliance_percent", result["compliance_percent"])

    # Scan errors count
    if "scan_errors" in result:
        add_number("scan_errors", result["scan_errors"])

    # Delivery errors count
    if "delivery_errors" in result:
        add_number("delivery_errors", result["delivery_errors"])

    # Status
    if "status" in result:
        add_string("status", result["status"])

    # No metrics count
    if "no_metrics" in result:
        add_number("no_metrics", result["no_metrics"])

    # Stable event identity lets subscribers deduplicate at-least-once delivery.
    if result.get("run_id"):
        add_string("run_id", result["run_id"], 64)

    return attrs


def send_report(report, result=None):
    """Send report via SNS with optional message attributes for filtering.

    Truncates oversized messages to the SNS 256KB limit with a pointer to S3.
    Returns the SNS MessageId.
    """
    topic_arn = os.environ.get("SNS_TOPIC_ARN")
    if not topic_arn:
        logger.error("SNS_TOPIC_ARN environment variable is not set")
        raise ValueError("SNS_TOPIC_ARN is not set")
    if not re.match(r"^arn:aws[a-z-]*:sns:[a-z0-9-]+:\d{12}:[^:]+$", topic_arn):
        logger.warning(f"SNS_TOPIC_ARN has unexpected format: {topic_arn}")

    if not isinstance(report, str) or not report:
        raise ValueError("report must be a non-empty string")

    sns = _get_sns_client()

    logger.info(f"Sending report to SNS topic {topic_arn}")

    message = report
    encoded = message.encode("utf-8")
    attrs = _build_message_attributes(result) if result else {}
    attr_bytes = sum(
        len(k.encode()) + len(v["DataType"].encode()) + len(v["StringValue"].encode())
        for k, v in attrs.items()
    )
    message_limit = SNS_MAX_BYTES - attr_bytes - 1024
    if len(encoded) > message_limit:
        s3_hint = os.environ.get("REPORT_BUCKET", "")
        note = (
            f"\n\n[TRUNCATED for SNS 256KB limit; full report in S3 bucket {s3_hint} ]"
        )
        budget = message_limit - len(note.encode("utf-8"))
        truncated = encoded[: max(budget, 0)].decode("utf-8", errors="ignore")
        message = truncated + note
        logger.warning(
            f"SNS message truncated from {len(encoded)} to {len(message.encode('utf-8'))} bytes"
        )

    # Subject must be ASCII-safe-ish and <=100 chars; weekly->generic schedule.
    schedule = os.environ.get("REPORT_SCHEDULE", "Weekly")
    subject = "".join(
        c
        for c in f"AWS Waste Hunter - {schedule} Cost Optimization Report"
        if 32 <= ord(c) < 127
    )[:99]

    publish_params = {
        "TopicArn": topic_arn,
        "Subject": subject,
        "Message": message,
    }

    # Add message attributes if result provided
    if result:
        attrs = _build_message_attributes(result)
        if attrs:
            publish_params["MessageAttributes"] = attrs

    response = sns.publish(**publish_params)
    message_id = response.get("MessageId", "") if isinstance(response, dict) else ""
    logger.info(
        f"Report sent successfully via SNS (MessageId={message_id or 'unknown'})"
    )
    _ = uuid
    return message_id
