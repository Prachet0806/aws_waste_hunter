"""Isolated scanner Lambda. Large data leaves the workflow through S3 references."""
import os
import time

from delivery.s3_archiver import archive_json
from lambda_handler import run_scanner, scanners
from utils.config import Config
from utils.run_store import RunStore, release_on_failure


class RetryableScanError(RuntimeError):
    pass


def handler(event, context):
    config = Config(**event["config"])
    name = event["scanner"]
    registry = dict(scanners())
    if name not in registry:
        raise ValueError("Unknown scanner")
    store = RunStore()
    token, previous = store.acquire(event["run_id"], name)
    if previous is not None:
        return previous
    with release_on_failure(store, event["run_id"], name, token):
        budget = min(config.scan_seconds, context.get_remaining_time_in_millis()/1000-15)
        result = run_scanner(name, registry[name], config, event["account_id"], time.monotonic()+max(0, budget))
        from utils.logging_config import emit_scanner_metrics
        prefix = os.getenv("REPORT_PREFIX", "reports").strip("/")
        key = f"{prefix}/{event['account_id']}/{config.region}/{event['run_id']}/attempts/{name}/{token}.json"
        archive_json(result, key, on_encoded=lambda size: emit_scanner_metrics(result, size))
        output = {"scanner": name, "bucket": os.environ["REPORT_BUCKET"], "key": key, "status": result["status"]}
        if any(e.get("retryable") for e in result.get("errors", [])):
            raise RetryableScanError("Transient scanner failure; attempt evidence archived")
        store.finish(event["run_id"], name, token, output)
        return output
