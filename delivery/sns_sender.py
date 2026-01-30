import boto3
import os
import logging
from utils.aws_helpers import BOTO3_CONFIG

logger = logging.getLogger(__name__)

_sns_client = None


def _get_sns_client():
    """Lazy initialization of SNS client."""
    global _sns_client
    if _sns_client is None:
        region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION"))
        _sns_client = boto3.client("sns", region_name=region, config=BOTO3_CONFIG)
    return _sns_client


def send_report(report):
    """Send report via SNS."""
    topic_arn = os.environ.get("SNS_TOPIC_ARN")
    if not topic_arn:
        logger.error("SNS_TOPIC_ARN environment variable is not set")
        raise ValueError("SNS_TOPIC_ARN is not set")
    
    sns = _get_sns_client()
    
    logger.info(f"Sending report to SNS topic {topic_arn}")
    sns.publish(
        TopicArn=topic_arn,
        Subject="AWS Waste Hunter – Weekly Cost Optimization Report",
        Message=report,
    )
    logger.info("Report sent successfully via SNS")
