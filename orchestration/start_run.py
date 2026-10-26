"""Start a named Standard execution; retries of an event reuse its identity."""

import hashlib
import json
import os
import uuid
from dataclasses import asdict

from utils.aws_factory import get_client
from utils.config import Config


def handler(event, context):
    config = Config.load()
    account = get_client("sts").get_caller_identity()["Account"]
    identity = event.get("idempotency_key") or event.get("id") or uuid.uuid4().hex
    redrive_key = event.get("redrive_key")
    if redrive_key is not None and (
        not isinstance(redrive_key, str) or not redrive_key.strip()
    ):
        raise ValueError("redrive_key must be a non-empty string")
    generation = f"{identity}/redrive/{redrive_key}" if redrive_key else str(identity)
    run_id = hashlib.sha256(
        f"{account}/{config.region}/{generation}".encode()
    ).hexdigest()
    payload = {
        "schema_version": 2,
        "run_id": run_id,
        "account_id": account,
        "config": asdict(config),
        "event_time": event.get("time"),
        "scanners": [
            "scan_unattached_ebs",
            "scan_idle_ec2",
            "scan_unused_elb",
            "scan_stopped_rds",
            "scan_idle_rds",
            "scan_unattached_eips",
            "scan_orphaned_snapshots",
        ],
    }
    client = get_client("stepfunctions")
    arn = os.environ["STATE_MACHINE_ARN"]
    try:
        result = client.start_execution(
            stateMachineArn=arn, name=run_id, input=json.dumps(payload)
        )
        execution = result["executionArn"]
    except client.exceptions.ExecutionAlreadyExists:
        execution = arn.replace(":stateMachine:", ":execution:") + ":" + run_id
        state = client.describe_execution(executionArn=execution)
        execution_status = state.get("status")
        if execution_status == "SUCCEEDED":
            return {
                "schema_version": 2,
                "status": "completed",
                "run_id": run_id,
                "execution_arn": execution,
            }
        if execution_status not in {"RUNNING", "PENDING", "STOPPING"}:
            return {
                "schema_version": 2,
                "status": "failed",
                "run_id": run_id,
                "execution_arn": execution,
                "execution_status": execution_status or "UNKNOWN",
                "redrive_required": True,
            }
    return {
        "schema_version": 2,
        "status": "accepted",
        "run_id": run_id,
        "execution_arn": execution,
    }
