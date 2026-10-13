"""Read isolated task outputs, finalize evidence, and persist final run status."""
import json
import os
import time
from utils.aws_factory import get_client
from utils.config import Config
from utils.run_store import RunStore, release_on_failure
from lambda_handler import finalize


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
        for output in event["outputs"]:
            if "key" not in output:
                results.append({"scanner": output["scanner"], "status": "failed", "findings": [], "inventory": [],
                                "errors": [{"stage": output["scanner"], "type": "TaskFailure", "error": "isolated task failed; inspect execution history"}]})
                continue
            if output["bucket"] != os.environ["REPORT_BUCKET"] or not output["key"].startswith(expected_prefix):
                raise ValueError("Artifact reference outside the run scope")
            response = s3.get_object(Bucket=output["bucket"], Key=output["key"])
            with response["Body"] as body:
                results.append(json.loads(body.read()))
        result = finalize(results, config, event["account_id"], event["run_id"],
                          time.monotonic()+max(0, context.get_remaining_time_in_millis()/1000-5))
        store.finish(event["run_id"], "finalize", token, result)
        # Terminal application outcomes are explicit; the workflow chooses Fail.
        return result
