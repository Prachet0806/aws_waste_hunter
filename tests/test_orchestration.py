import io
import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from orchestration import finalize_run, scanner_handler, start_run
from utils.config import Config
from utils.run_store import RunStore


def event():
    return {
        "run_id": "run",
        "account_id": "123456789012",
        "config": asdict(Config.load()),
        "scanner": "scan_unattached_eips",
    }


def test_start_event_identity_is_stable(monkeypatch):
    states = Mock(start_execution=Mock(return_value={"executionArn": "execution"}))
    monkeypatch.setenv(
        "STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123456789012:stateMachine:test"
    )
    monkeypatch.setattr(
        start_run,
        "get_client",
        lambda svc: (
            states
            if svc == "stepfunctions"
            else Mock(
                get_caller_identity=Mock(return_value={"Account": "123456789012"})
            )
        ),
    )
    first = start_run.handler({"id": "event"}, None)
    second = start_run.handler({"id": "event"}, None)
    assert first["run_id"] == second["run_id"] and first["status"] == "accepted"
    assert states.start_execution.call_args.kwargs["name"] == first["run_id"]


def test_duplicate_execution_returns_existing_reference(monkeypatch):
    class Exists(Exception):
        pass

    states = Mock(
        exceptions=SimpleNamespace(ExecutionAlreadyExists=Exists),
        describe_execution=Mock(return_value={"status": "RUNNING"}),
    )
    states.start_execution.side_effect = Exists()
    monkeypatch.setenv(
        "STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123456789012:stateMachine:test"
    )
    monkeypatch.setattr(
        start_run,
        "get_client",
        lambda svc: (
            states
            if svc == "stepfunctions"
            else Mock(
                get_caller_identity=Mock(return_value={"Account": "123456789012"})
            )
        ),
    )
    result = start_run.handler({"id": "event"}, None)
    assert (
        ":execution:test:" in result["execution_arn"] and result["status"] == "accepted"
    )


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("SUCCEEDED", "completed"),
        ("FAILED", "failed"),
        ("TIMED_OUT", "failed"),
        ("ABORTED", "failed"),
    ],
)
def test_duplicate_execution_reports_terminal_state(monkeypatch, state, expected):
    class Exists(Exception):
        pass

    states = Mock(
        exceptions=SimpleNamespace(ExecutionAlreadyExists=Exists),
        describe_execution=Mock(return_value={"status": state}),
    )
    states.start_execution.side_effect = Exists()
    monkeypatch.setenv(
        "STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123456789012:stateMachine:test"
    )
    monkeypatch.setattr(
        start_run,
        "get_client",
        lambda svc: (
            states
            if svc == "stepfunctions"
            else Mock(
                get_caller_identity=Mock(return_value={"Account": "123456789012"})
            )
        ),
    )
    result = start_run.handler({"id": "event"}, None)
    assert result["status"] == expected
    if state in {"FAILED", "TIMED_OUT", "ABORTED"}:
        assert result["redrive_required"] is True


def test_redrive_key_creates_stable_new_execution_identity(monkeypatch):
    states = Mock(start_execution=Mock(return_value={"executionArn": "execution"}))
    monkeypatch.setenv(
        "STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123456789012:stateMachine:test"
    )
    monkeypatch.setattr(
        start_run,
        "get_client",
        lambda svc: (
            states
            if svc == "stepfunctions"
            else Mock(
                get_caller_identity=Mock(return_value={"Account": "123456789012"})
            )
        ),
    )
    first = start_run.handler({"id": "event", "redrive_key": "retry-1"}, None)
    payload = json.loads(states.start_execution.call_args.kwargs["input"])
    second = start_run.handler({"id": "event", "redrive_key": "retry-1"}, None)
    assert first["run_id"] == second["run_id"] == payload["run_id"]
    assert first["run_id"] != start_run.handler({"id": "event"}, None)["run_id"]


def test_scanner_artifact_precedes_acknowledgment_and_reports_size(monkeypatch, capsys):
    store = Mock(acquire=Mock(return_value=("lease", None)))
    monkeypatch.setattr(scanner_handler, "RunStore", lambda: store)
    monkeypatch.setenv("REPORT_BUCKET", "reports")
    scanned = {
        "scanner": "scan_unattached_eips",
        "status": "complete",
        "findings": [],
        "inventory": [],
        "errors": [],
    }
    monkeypatch.setattr(scanner_handler, "run_scanner", Mock(return_value=scanned))
    archive = Mock(side_effect=lambda value, key, **kwargs: kwargs["on_encoded"](789))
    monkeypatch.setattr(scanner_handler, "archive_json", archive)
    result = scanner_handler.handler(
        event(), SimpleNamespace(get_remaining_time_in_millis=lambda: 270000)
    )
    assert "/attempts/scan_unattached_eips/lease.json" in result["key"]
    assert archive.call_args.args[0] == scanned
    assert json.loads(capsys.readouterr().out)["ScannerArtifactBytes"] == 789
    store.finish.assert_called_once()


def test_cached_scanner_does_not_repeat_work(monkeypatch):
    monkeypatch.setattr(
        scanner_handler,
        "RunStore",
        lambda: Mock(acquire=Mock(return_value=(None, {"key": "prior"}))),
    )
    scanner = Mock()
    monkeypatch.setattr(scanner_handler, "run_scanner", scanner)
    assert scanner_handler.handler(event(), None) == {"key": "prior"}
    scanner.assert_not_called()


def test_transient_scanner_errors_release_lease_and_retry(monkeypatch):
    store = Mock(acquire=Mock(return_value=("lease", None)))
    monkeypatch.setattr(scanner_handler, "RunStore", lambda: store)
    monkeypatch.setenv("REPORT_BUCKET", "reports")
    monkeypatch.setattr(
        scanner_handler,
        "run_scanner",
        Mock(
            return_value={
                "scanner": "scan_unattached_eips",
                "status": "failed",
                "errors": [{"retryable": True}],
            }
        ),
    )
    monkeypatch.setattr(scanner_handler, "archive_json", Mock())
    with pytest.raises(scanner_handler.RetryableScanError):
        scanner_handler.handler(
            event(), SimpleNamespace(get_remaining_time_in_millis=lambda: 270000)
        )
    store.release.assert_called_once()
    store.finish.assert_not_called()


def test_finalizer_reads_scoped_artifacts_and_catches_task_failure(monkeypatch):
    store = Mock(acquire=Mock(return_value=("lease", None)))
    monkeypatch.setattr(finalize_run, "RunStore", lambda: store)
    monkeypatch.setenv("REPORT_BUCKET", "reports")
    content = json.dumps(
        {
            "scanner": "one",
            "status": "complete",
            "inventory": [],
            "findings": [],
            "errors": [],
        }
    ).encode()
    s3 = Mock(
        head_object=Mock(return_value={"ContentLength": len(content)}),
        get_object=Mock(return_value={"Body": io.BytesIO(content)}),
    )
    monkeypatch.setattr(finalize_run, "get_client", lambda svc: s3)
    finish = Mock(return_value={"status": "partial"})
    monkeypatch.setattr(finalize_run, "finalize", finish)
    ev = event()
    ev["outputs"] = [
        {
            "scanner": "one",
            "bucket": "reports",
            "key": "reports/123456789012/us-east-1/run/attempts/one/lease.json",
        },
        {"scanner": "two", "status": "failed"},
    ]
    result = finalize_run.handler(
        ev, SimpleNamespace(get_remaining_time_in_millis=lambda: 240000)
    )
    assert (
        result["status"] == "partial"
        and finish.call_args.args[0][1]["status"] == "failed"
    )
    store.finish.assert_called_once()


def test_foreign_artifact_rejected(monkeypatch):
    monkeypatch.setattr(
        finalize_run,
        "RunStore",
        lambda: Mock(acquire=Mock(return_value=("lease", None))),
    )
    monkeypatch.setattr(finalize_run, "get_client", Mock())
    monkeypatch.setenv("REPORT_BUCKET", "reports")
    ev = event()
    ev["outputs"] = [{"scanner": "one", "bucket": "other", "key": "foreign"}]
    with pytest.raises(ValueError, match="outside"):
        finalize_run.handler(ev, None)


def test_finalizer_skips_oversized_artifact_without_reading_body(monkeypatch):
    store = Mock(acquire=Mock(return_value=("lease", None)))
    monkeypatch.setattr(finalize_run, "RunStore", lambda: store)
    monkeypatch.setenv("REPORT_BUCKET", "reports")
    s3 = Mock(head_object=Mock(return_value={"ContentLength": 9 * 1024 * 1024}))
    monkeypatch.setattr(finalize_run, "get_client", lambda _: s3)
    monkeypatch.setattr(
        finalize_run, "finalize", lambda results, *args, **kwargs: results
    )
    ev = event()
    ev["outputs"] = [
        {
            "scanner": "large",
            "bucket": "reports",
            "key": "reports/123456789012/us-east-1/run/attempts/large/attempt.json",
        }
    ]
    output = finalize_run.handler(
        ev, SimpleNamespace(get_remaining_time_in_millis=lambda: 240000)
    )
    assert (
        output[0]["status"] == "failed"
        and output[0]["errors"][0]["type"] == "ArtifactSizeLimit"
    )
    s3.get_object.assert_not_called()


def test_conditional_run_store_cache_and_lease(monkeypatch):
    class Conflict(Exception):
        pass

    client = Mock(exceptions=SimpleNamespace(ConditionalCheckFailedException=Conflict))
    monkeypatch.setenv("RUN_TABLE", "runs")
    store = RunStore(client)
    token, prior = store.acquire("run", "stage")
    assert token and prior is None
    store.finish("run", "stage", token, {"status": "complete"})
    assert (
        client.update_item.call_args.kwargs["ConditionExpression"] == "#token = :token"
    )
    store.release("run", "stage", token)
    assert (
        client.update_item.call_args.kwargs["ConditionExpression"]
        == "#token = :token AND #status <> :complete"
    )
    client.put_item.side_effect = Conflict()
    client.get_item.return_value = {
        "Item": {"status": {"S": "complete"}, "result": {"S": '{"status":"complete"}'}}
    }
    assert store.acquire("run", "stage") == (None, {"status": "complete"})
    client.get_item.return_value = {"Item": {"status": {"S": "running"}}}
    with pytest.raises(RuntimeError, match="already running"):
        store.acquire("run", "stage")


def test_artifact_build_allowlist_and_workflow_contract(tmp_path):
    from scripts.build_package import build

    root = build(tmp_path / "artifact", dependencies=False)
    assert (root / "lambda_handler.py").is_file()
    assert (root / "orchestration/finalize_run.py").is_file()
    assert not (root / "tests").exists() and not (root / ".git").exists()
    from pathlib import Path

    import yaml

    template = yaml.safe_load(Path("infra/template.yaml").read_text(encoding="utf-8"))
    workflow = json.loads(Path("infra/workflow.asl.json").read_text())
    assert template["Resources"]["Workflow"]["Properties"]["Type"] == "STANDARD"
    assert workflow["States"]["Scan"]["MaxConcurrency"] == 4
    assert (
        template["Resources"]["ExecutionTimeoutAlarm"]["Properties"]["MetricName"]
        == "ExecutionsTimedOut"
    )
    assert any(
        "RetryableStageError" in rule["ErrorEquals"]
        for rule in workflow["States"]["Finalize"]["Retry"]
    )
    assert (
        "AWS_REGION"
        not in template["Resources"]["ScannerFunction"]["Properties"]["Environment"][
            "Variables"
        ]
    )
    statements = template["Resources"]["FinalizerRole"]["Properties"]["Policies"][0][
        "PolicyDocument"
    ]["Statement"]
    assert next(s for s in statements if "sns:Publish" in s.get("Action", []))[
        "Resource"
    ] == {"Ref": "ReportTopic"}
    assert "OperationsDashboard" in template["Resources"]
    dashboard_body = template["Resources"]["OperationsDashboard"]["Properties"][
        "DashboardBody"
    ]["Fn::Sub"]
    assert (
        "FinalizerInputBytes" in dashboard_body
        and "ArtifactSizeRejections" in dashboard_body
    )
    assert "ScannerArtifactBytes" in dashboard_body
    assert "2 MiB aggregate read cap" in dashboard_body
    dashboard = json.loads(
        dashboard_body.replace("${AWS::Region}", "us-east-1").replace(
            "${AWS::StackName}", "test"
        )
    )
    assert len(dashboard["widgets"]) == 6
    assert template["Parameters"]["FinalizerInputBytes"]["MaxValue"] == 2097152


def test_finalizer_read_failure_releases_lease_for_transient_retry(monkeypatch):
    from botocore.exceptions import ClientError

    from utils.run_store import RetryableStageError

    store = Mock(acquire=Mock(return_value=("lease", None)))
    monkeypatch.setattr(finalize_run, "RunStore", lambda: store)
    monkeypatch.setenv("REPORT_BUCKET", "reports")
    client = Mock()
    client.head_object.return_value = {"ContentLength": 1}
    client.get_object.side_effect = ClientError(
        {"Error": {"Code": "SlowDown"}}, "GetObject"
    )
    monkeypatch.setattr(finalize_run, "get_client", lambda svc: client)
    ev = event()
    ev["outputs"] = [
        {
            "scanner": "one",
            "bucket": "reports",
            "key": "reports/123456789012/us-east-1/run/attempts/one/lease.json",
        }
    ]
    with pytest.raises(RetryableStageError):
        finalize_run.handler(ev, None)
    store.release.assert_called_once_with("run", "finalize", "lease")
    store.finish.assert_not_called()


def test_scanner_ack_failure_releases_lease(monkeypatch):
    store = Mock(acquire=Mock(return_value=("lease", None)))
    store.finish.side_effect = RuntimeError("ack failed")
    monkeypatch.setattr(scanner_handler, "RunStore", lambda: store)
    monkeypatch.setenv("REPORT_BUCKET", "reports")
    monkeypatch.setattr(
        scanner_handler,
        "run_scanner",
        Mock(
            return_value={
                "scanner": "scan_unattached_eips",
                "status": "complete",
                "errors": [],
            }
        ),
    )
    monkeypatch.setattr(scanner_handler, "archive_json", Mock())
    with pytest.raises(RuntimeError, match="ack failed"):
        scanner_handler.handler(
            event(), SimpleNamespace(get_remaining_time_in_millis=lambda: 270000)
        )
    store.release.assert_called_once_with("run", "scan_unattached_eips", "lease")


def test_release_failure_preserves_original_exception():
    from utils.run_store import release_on_failure

    store = Mock(release=Mock(side_effect=RuntimeError("release failed")))
    with pytest.raises(ValueError, match="original"):
        with release_on_failure(store, "run", "stage", "lease"):
            raise ValueError("original")


@pytest.mark.parametrize(
    ("resources_per_scanner", "scanner_count", "expected_failed"),
    [
        (10, 7, 0),  # small account: all seven scanner artifacts are far below budget
        (
            1000,
            7,
            1,
        ),  # medium profile: aggregate crosses 2 MiB, so the last artifact is rejected
        (10000, 1, 1),  # large single-scan profile: one artifact exceeds 2 MiB
    ],
)
def test_finalizer_synthetic_account_size_profiles(
    monkeypatch, capsys, resources_per_scanner, scanner_count, expected_failed
):
    """Measure realistic-shaped JSON artifacts against the aggregate read cap (offline only)."""
    store = Mock(acquire=Mock(return_value=("lease", None)))
    monkeypatch.setattr(finalize_run, "RunStore", lambda: store)
    monkeypatch.setenv("REPORT_BUCKET", "reports")
    monkeypatch.setenv(
        "MAX_FINALIZER_INPUT_BYTES", str(Config.load().max_finalizer_total_bytes)
    )

    artifacts = {}
    outputs = []
    for scanner_index in range(scanner_count):
        scanner = f"scanner-{scanner_index}"
        inventory = [
            {
                "id": f"vol-{scanner_index:02d}-{resource_index:05d}",
                "resource_key": f"ebs:us-east-1:123456789012:vol-{scanner_index:02d}-{resource_index:05d}",
                "type": "EBS",
                "region": "us-east-1",
                "account_id": "123456789012",
                "tags": {
                    "owner": "platform",
                    "environment": "production",
                    "cost-center": "1234",
                },
                "state": "available",
                "size_gb": 100,
                "created": "2026-01-01T00:00:00Z",
            }
            for resource_index in range(resources_per_scanner)
        ]
        artifact = json.dumps(
            {
                "scanner": scanner,
                "status": "complete",
                "findings": [],
                "inventory": inventory,
                "errors": [],
                "operations": {"describe_volumes": True},
                "operation_details": {
                    "describe_volumes": {
                        "required": True,
                        "attempts": 1,
                        "status": "complete",
                    }
                },
            },
            allow_nan=False,
        ).encode()
        key = f"reports/123456789012/us-east-1/run/attempts/{scanner}/attempt.json"
        artifacts[key] = artifact
        outputs.append({"scanner": scanner, "bucket": "reports", "key": key})

    s3 = Mock(
        head_object=Mock(
            side_effect=lambda Bucket, Key: {"ContentLength": len(artifacts[Key])}
        ),
        get_object=Mock(
            side_effect=lambda Bucket, Key: {"Body": io.BytesIO(artifacts[Key])}
        ),
    )
    monkeypatch.setattr(finalize_run, "get_client", lambda _: s3)
    captured = Mock(side_effect=lambda results, *args, **kwargs: results)
    monkeypatch.setattr(finalize_run, "finalize", captured)
    ev = event()
    ev["outputs"] = outputs

    results = finalize_run.handler(
        ev, SimpleNamespace(get_remaining_time_in_millis=lambda: 240000)
    )

    failed = [result for result in results if result["status"] == "failed"]
    total_bytes = sum(map(len, artifacts.values()))
    budget = Config.load().max_finalizer_total_bytes
    metrics = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert len(failed) == expected_failed
    assert all(result["errors"][0]["type"] == "ArtifactSizeLimit" for result in failed)
    assert s3.get_object.call_count == scanner_count - expected_failed
    assert (total_bytes <= budget) == (expected_failed == 0)
    assert metrics["FinalizerInputBytes"] == total_bytes
    assert metrics["ArtifactSizeRejections"] == expected_failed


def test_concurrent_duplicate_starter_invocations_only_start_one_execution(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    class Exists(Exception):
        pass

    class AtomicStates:
        def __init__(self):
            self.exceptions = SimpleNamespace(ExecutionAlreadyExists=Exists)
            self.lock = threading.Lock()
            self.executions = {}
            self.starts = 0
            self.start_barrier = threading.Barrier(2)

        def start_execution(self, *, stateMachineArn, name, input):
            self.start_barrier.wait(timeout=5)
            with self.lock:
                self.starts += 1
                if name in self.executions:
                    raise Exists()
                self.executions[name] = json.loads(input)
                return {"executionArn": f"{stateMachineArn}:execution:{name}"}

        def describe_execution(self, *, executionArn):
            return {"status": "RUNNING"}

    states = AtomicStates()
    monkeypatch.setenv(
        "STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123456789012:stateMachine:test"
    )
    monkeypatch.setattr(
        start_run,
        "get_client",
        lambda svc: (
            states
            if svc == "stepfunctions"
            else Mock(
                get_caller_identity=Mock(return_value={"Account": "123456789012"})
            )
        ),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: start_run.handler({"id": "same-event"}, None), range(2))
        )

    assert [result["status"] for result in results] == ["accepted", "accepted"]
    assert results[0]["run_id"] == results[1]["run_id"]
    assert states.starts == 2
    assert len(states.executions) == 1


def test_run_store_lease_expiry_allows_recovery_only_after_900_seconds(monkeypatch):
    class Conflict(Exception):
        pass

    client = Mock(exceptions=SimpleNamespace(ConditionalCheckFailedException=Conflict))
    client.put_item.side_effect = [Conflict(), None]
    client.get_item.return_value = {"Item": {"status": {"S": "running"}}}
    monkeypatch.setenv("RUN_TABLE", "runs")
    monkeypatch.setattr("utils.run_store.time.time", Mock(side_effect=[1000, 1901]))
    store = RunStore(client)

    with pytest.raises(RuntimeError, match="lease expires"):
        store.acquire("run", "scanner")
    token, previous = store.acquire("run", "scanner")

    assert token and previous is None
    assert client.put_item.call_args_list[0].kwargs["Item"]["lease"]["N"] == "1900"
    assert (
        client.put_item.call_args_list[1].kwargs["ExpressionAttributeValues"][":now"][
            "N"
        ]
        == "1901"
    )
    assert (
        "lease < :now"
        in client.put_item.call_args_list[1].kwargs["ConditionExpression"]
    )


def test_concurrent_run_store_claims_grant_only_one_stage_lease(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    class Conflict(Exception):
        pass

    class AtomicTable:
        def __init__(self):
            self.exceptions = SimpleNamespace(ConditionalCheckFailedException=Conflict)
            self.lock = threading.Lock()
            self.barrier = threading.Barrier(2)
            self.items = {}

        def put_item(self, **request):
            self.barrier.wait(timeout=5)
            item = request["Item"]
            with self.lock:
                current = self.items.get(item["pk"]["S"])
                if current and int(current["lease"]["N"]) >= int(
                    request["ExpressionAttributeValues"][":now"]["N"]
                ):
                    raise Conflict()
                self.items[item["pk"]["S"]] = item

        def get_item(self, **request):
            return {"Item": self.items.get(request["Key"]["pk"]["S"], {})}

    monkeypatch.setenv("RUN_TABLE", "runs")
    client = AtomicTable()

    def claim():
        try:
            return RunStore(client).acquire("run", "finalize")[0]
        except RuntimeError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))

    assert (
        sum(
            isinstance(result, str) and "already running" in result
            for result in results
        )
        == 1
    )
    assert (
        sum(
            isinstance(result, str) and "already running" not in result
            for result in results
        )
        == 1
    )


@pytest.mark.parametrize("failure_stage", ["s3", "sns", "manifest"])
def test_transient_finalizer_side_effect_is_retried_without_terminal_ack(
    monkeypatch, failure_stage
):
    from botocore.exceptions import ClientError

    import lambda_handler as app
    from delivery import sns_sender
    from utils.run_store import RetryableStageError

    store = Mock(acquire=Mock(side_effect=[("lease-1", None), ("lease-2", None)]))
    monkeypatch.setattr(finalize_run, "RunStore", lambda: store)
    monkeypatch.setattr(finalize_run, "finalize", app.finalize)
    monkeypatch.setenv("REPORT_BUCKET", "reports")
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:reports")
    monkeypatch.setenv("RUN_TABLE", "runs")

    artifact = json.dumps(
        {
            "scanner": "one",
            "status": "complete",
            "findings": [],
            "inventory": [],
            "errors": [],
        }
    ).encode()
    s3 = Mock(
        head_object=Mock(return_value={"ContentLength": len(artifact)}),
        get_object=Mock(side_effect=lambda **_: {"Body": io.BytesIO(artifact)}),
    )
    monkeypatch.setattr(finalize_run, "get_client", lambda _: s3)
    write_calls = []

    def archive_json(value, key, **kwargs):
        write_calls.append(key)
        if failure_stage == "s3" and len(write_calls) == 1:
            raise ClientError({"Error": {"Code": "SlowDown"}}, "PutObject")
        if (
            failure_stage == "manifest"
            and key.endswith("/manifest.json")
            and write_calls.count(key) == 1
        ):
            raise ClientError({"Error": {"Code": "SlowDown"}}, "PutObject")
        return key

    monkeypatch.setattr(app, "archive_json", archive_json)
    monkeypatch.setattr(
        app, "archive_report", Mock(return_value="reports/run/report.md")
    )
    publish_attempts = []

    class FlakySns:
        def publish(self, **params):
            publish_attempts.append(params)
            if failure_stage == "sns" and len(publish_attempts) == 1:
                # The service may have accepted the request before the client sees this error.
                raise ClientError({"Error": {"Code": "SlowDown"}}, "Publish")
            return {"MessageId": f"message-{len(publish_attempts)}"}

    monkeypatch.setattr(sns_sender, "get_client", lambda *_args, **_kwargs: FlakySns())
    ev = event()
    ev["run_id"] = "stable-run"
    ev["outputs"] = [
        {
            "scanner": "one",
            "bucket": "reports",
            "key": "reports/123456789012/us-east-1/stable-run/attempts/one/lease.json",
        }
    ]
    context = SimpleNamespace(get_remaining_time_in_millis=lambda: 240000)

    with pytest.raises(RetryableStageError):
        finalize_run.handler(ev, context)
    store.release.assert_called_once_with("stable-run", "finalize", "lease-1")
    store.finish.assert_not_called()

    result = finalize_run.handler(ev, context)

    assert result["delivery"]["notification"] is True
    store.finish.assert_called_once_with("stable-run", "finalize", "lease-2", result)
    if failure_stage in {"sns", "manifest"}:
        assert len(publish_attempts) == 2
        assert [
            p["MessageAttributes"]["run_id"]["StringValue"] for p in publish_attempts
        ] == ["stable-run", "stable-run"]


def test_artifact_smoke_rejects_missing_dependencies(tmp_path):
    import subprocess

    from scripts.build_package import build
    from scripts.validate_artifact import validate

    root = build(tmp_path / "artifact", dependencies=False)
    with pytest.raises(subprocess.CalledProcessError):
        validate(root)
