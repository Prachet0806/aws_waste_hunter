import boto3
import os

sns = boto3.client("sns")

def send_report(report):
    topic_arn = os.environ.get("SNS_TOPIC_ARN")
    if not topic_arn:
        raise ValueError("SNS_TOPIC_ARN is not set")
    sns.publish(
        TopicArn=topic_arn,
        Subject="AWS Waste Hunter – Weekly Cost Optimization Report",
        Message=report,
    )
