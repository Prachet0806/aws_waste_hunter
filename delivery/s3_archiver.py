import boto3
import datetime
import os
import logging
from utils.aws_helpers import BOTO3_CONFIG

logger = logging.getLogger(__name__)

_s3_client = None


def _get_s3_client():
    """Lazy initialization of S3 client."""
    global _s3_client
    if _s3_client is None:
        region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION"))
        _s3_client = boto3.client("s3", region_name=region, config=BOTO3_CONFIG)
    return _s3_client


def archive_report(report):
    """Archive report to S3."""
    bucket = os.environ.get("REPORT_BUCKET")
    if not bucket:
        logger.error("REPORT_BUCKET environment variable is not set")
        raise ValueError("REPORT_BUCKET is not set")
    
    s3 = _get_s3_client()
    key = f"reports/{datetime.date.today()}.md"
    
    logger.info(f"Archiving report to s3://{bucket}/{key}")
    s3.put_object(Bucket=bucket, Key=key, Body=report)
    logger.info("Report archived successfully to S3")
