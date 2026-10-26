"""Explicit opt-in: invokes a designated staging stack and writes report artifacts."""

import json
import os
import time
import uuid

import boto3
import pytest

pytestmark = [
    pytest.mark.staging,
    pytest.mark.skipif(
        os.getenv("AWS_INTEGRATION_ENABLED") != "true",
        reason="Deployed AWS verification requires explicit opt-in",
    ),
]


def test_staging_execution_and_event_idempotency():
    session = boto3.Session(region_name=os.environ["STAGING_REGION"])
    account = session.client("sts").get_caller_identity()["Account"]
    assert account == os.environ["STAGING_ACCOUNT_ID"], (
        "Account mismatch; invocation suppressed"
    )
    function = os.environ["STARTER_FUNCTION_NAME"]
    assert "staging" in function.lower(), "Use an explicitly named staging stack"
    event = {"idempotency_key": "staging-test-" + uuid.uuid4().hex}
    lamb = session.client("lambda")
    runs = []
    for _ in range(2):
        response = lamb.invoke(
            FunctionName=function, Payload=json.dumps(event).encode()
        )
        assert "FunctionError" not in response
        with response["Payload"] as body:
            runs.append(json.loads(body.read()))
    assert runs[0]["execution_arn"] == runs[1]["execution_arn"]
    states = session.client("stepfunctions")
    for _ in range(90):
        execution = states.describe_execution(executionArn=runs[0]["execution_arn"])
        if execution["status"] != "RUNNING":
            break
        time.sleep(10)
    assert execution["status"] == "SUCCEEDED", execution
    result = json.loads(execution["output"])
    assert result["schema_version"] == 2
    assert len(result["scanner_outcomes"]) == 7
    assert result["delivery"]["archive"] and result["delivery"]["notification"]
    uri = result["archive_uri"].removeprefix("s3://")
    bucket, key = uri.split("/", 1)
    session.client("s3").head_object(Bucket=bucket, Key=key)
