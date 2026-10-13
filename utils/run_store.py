"""Conditional stage leases and cached acknowledgments, with at-least-once delivery."""
import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from utils.aws_factory import get_client


class RetryableStageError(RuntimeError):
    """A transient persistence failure after the owned lease was released."""


@contextmanager
def release_on_failure(store, run_id, stage, token):
    """Do not strand an owned stage when an invocation returns an exception."""
    try:
        yield
    except Exception as exc:
        try:
            store.release(run_id, stage, token)
        except Exception:
            logging.getLogger(__name__).exception("Could not release failed stage lease")
            # Retain the original failure; the lease remains safe until expiry.
            raise exc
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        transient = code in {"Throttling", "ThrottlingException", "RequestLimitExceeded", "SlowDown",
                             "InternalError", "InternalServerError", "ServiceUnavailable", "RequestTimeout"}
        transient = transient or type(exc).__name__ in {
            "EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError"}
        if transient:
            raise RetryableStageError("Transient persistence failure; stage lease released") from exc
        raise


class RunStore:
    def __init__(self, client=None):
        self.client = client or get_client("dynamodb")
        self.table = os.environ["RUN_TABLE"]

    def acquire(self, run_id, stage, seconds=900):
        key = f"{run_id}/{stage}"
        token = uuid.uuid4().hex
        now = int(time.time())
        try:
            self.client.put_item(TableName=self.table,
                Item={"pk": {"S": key}, "token": {"S": token}, "status": {"S": "running"},
                      "lease": {"N": str(now+seconds)}, "expires": {"N": str(now+30*86400)}},
                ConditionExpression="attribute_not_exists(pk) OR (#status <> :complete AND lease < :now)",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":complete": {"S": "complete"}, ":now": {"N": str(now)}})
            return token, None
        except self.client.exceptions.ConditionalCheckFailedException:
            item = self.client.get_item(TableName=self.table, Key={"pk": {"S": key}}, ConsistentRead=True).get("Item", {})
            if item.get("status", {}).get("S") == "complete":
                return None, json.loads(item["result"]["S"])
            raise RuntimeError("Stage already running; retry after its lease expires")

    def finish(self, run_id, stage, token, result):
        self.client.update_item(TableName=self.table, Key={"pk": {"S": f"{run_id}/{stage}"}},
            UpdateExpression="SET #status = :complete, #result = :result",
            ConditionExpression="#token = :token",
            ExpressionAttributeNames={"#status": "status", "#result": "result", "#token": "token"},
            ExpressionAttributeValues={":complete": {"S": "complete"}, ":token": {"S": token},
                                       ":result": {"S": json.dumps(result, allow_nan=False)}})

    def release(self, run_id, stage, token):
        self.client.update_item(TableName=self.table, Key={"pk": {"S": f"{run_id}/{stage}"}},
            UpdateExpression="SET lease = :zero, #status = :failed", ConditionExpression="#token = :token",
            ExpressionAttributeNames={"#status": "status", "#token": "token"},
            ExpressionAttributeValues={":zero": {"N": "0"}, ":failed": {"S": "retryable"}, ":token": {"S": token}})
