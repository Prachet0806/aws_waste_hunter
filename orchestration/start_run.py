"""Start a named Standard execution; retries of an event reuse its identity."""
from dataclasses import asdict
import hashlib
import json
import os
import uuid
from utils.aws_factory import get_client
from utils.config import Config


def handler(event, context):
    config = Config.load()
    account = get_client("sts").get_caller_identity()["Account"]
    identity = event.get("idempotency_key") or event.get("id") or uuid.uuid4().hex
    run_id = hashlib.sha256(f"{account}/{config.region}/{identity}".encode()).hexdigest()
    payload = {"schema_version": 2, "run_id": run_id, "account_id": account,
               "config": asdict(config), "event_time": event.get("time"),
               "scanners": ["scan_unattached_ebs", "scan_idle_ec2", "scan_unused_elb", "scan_stopped_rds",
                            "scan_idle_rds", "scan_unattached_eips", "scan_orphaned_snapshots"]}
    client = get_client("stepfunctions")
    arn = os.environ["STATE_MACHINE_ARN"]
    try:
        result = client.start_execution(stateMachineArn=arn, name=run_id, input=json.dumps(payload))
        execution = result["executionArn"]
    except client.exceptions.ExecutionAlreadyExists:
        execution = arn.replace(":stateMachine:", ":execution:")+":"+run_id
    return {"schema_version": 2, "status": "accepted", "run_id": run_id, "execution_arn": execution}
