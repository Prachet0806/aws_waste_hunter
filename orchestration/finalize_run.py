"""Read isolated task outputs, finalize evidence, and persist final run status."""
import json
import os
import time

from lambda_handler import finalize
from utils.aws_factory import get_client
from utils.config import Config
from utils.run_store import RunStore, release_on_failure


def handler(event, context):
    config = Config(**event["config"])
    store = RunStore()
    token, previous = store.acquire(event["run_id"], "finalize")
    if previous is not None:
        return previous
    with release_on_failure(store, event["run_id"], "finalize", token):
        results = []
        s3 = get_client("s3")
        expected_prefix = f"{os.getenv('REPORT_PREFIX', 'reports').strip('/')}/{event['account_id']}/{config.region}/{event['run_id']}/attempts/"
        remaining_bytes = config.max_finalizer_total_bytes
        observed_bytes = 0
        size_rejections = 0
        for output in event["outputs"]:
            if "key" not in output:
                results.append({"scanner": output["scanner"], "status": "failed", "findings": [], "inventory": [],
                                "errors": [{"stage": output["scanner"], "type": "TaskFailure", "error": "isolated task failed; inspect execution history"}]})
                continue
            if output["bucket"] != os.environ["REPORT_BUCKET"] or not output["key"].startswith(expected_prefix):
                raise ValueError("Artifact reference outside the run scope")
            size = s3.head_object(Bucket=output["bucket"], Key=output["key"]).get("ContentLength")
            if not isinstance(size, int) or size < 0:
                raise ValueError("Artifact size unavailable; refusing an unbounded read")
            observed_bytes += size
            if size > remaining_bytes:
                size_rejections += 1
                results.append({"scanner": output["scanner"], "status": "failed", "findings": [], "inventory": [],
                                "errors": [{"stage": "artifact_read", "type": "ArtifactSizeLimit",
                                            "error": f"artifact requires {size} bytes; remaining budget is {remaining_bytes}"}]})
                continue
            response = s3.get_object(Bucket=output["bucket"], Key=output["key"])
            with response["Body"] as body:
                content = body.read(remaining_bytes+1)
            if len(content) != size or len(content) > remaining_bytes:
                raise ValueError("Artifact size changed or exceeded the finalizer read budget")
            remaining_bytes -= len(content)
            results.append(json.loads(content))
        from utils.logging_config import emit_finalizer_metrics
        emit_finalizer_metrics(observed_bytes, size_rejections)
        result = finalize(results, config, event["account_id"], event["run_id"],
                          time.monotonic()+max(0, context.get_remaining_time_in_millis()/1000-5))
        store.finish(event["run_id"], "finalize", token, result)
        # Terminal application outcomes are explicit; the workflow chooses Fail.
        return result
