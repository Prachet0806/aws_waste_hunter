import boto3
import datetime
import os

s3 = boto3.client("s3")

def archive_report(report):
    bucket = os.environ.get("REPORT_BUCKET")
    if not bucket:
        raise ValueError("REPORT_BUCKET is not set")
    key = f"reports/{datetime.date.today()}.md"
    s3.put_object(Bucket=bucket, Key=key, Body=report)
