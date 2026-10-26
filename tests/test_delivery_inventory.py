import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from delivery import s3_archiver, sns_sender
from scanner.inventory import TrackedClient
from utils import aws_factory
from utils.config import Config
from utils.scan_context import ScanContext, activate


def test_factory_uses_task_snapshot_and_reuses_client(monkeypatch):
    raw = Mock(
        meta=SimpleNamespace(region_name="eu-west-1"),
        describe_addresses=Mock(return_value={"Addresses": []}),
    )
    session = Mock(client=Mock(return_value=raw))
    monkeypatch.setattr(aws_factory.boto3.session, "Session", lambda: session)
    from dataclasses import replace

    scope = ScanContext("test", replace(Config.load(), region="eu-west-1"))
    with activate(scope):
        first = aws_factory.get_client("ec2")
        assert aws_factory.get_client("ec2") is first
        assert first.meta.region_name == "eu-west-1"
        first.describe_addresses()
    assert session.client.call_args.kwargs["region_name"] == "eu-west-1"


def test_client_failure_records_incomplete_operation():
    raw = Mock(describe_addresses=Mock(side_effect=RuntimeError("denied")))
    scope = ScanContext("test", Config.load())
    with pytest.raises(RuntimeError):
        TrackedClient(raw, scope).describe_addresses()
    assert scope.operations["describe_addresses"] is False and scope.errors


def test_scan_operation_details_are_explicit_and_failed_calls_stay_incomplete():
    scope = ScanContext("test", Config.load())
    tracked = TrackedClient(
        Mock(describe_addresses=Mock(return_value={"Addresses": []})), scope
    )
    tracked.describe_addresses()
    result = scope.result([], 0)
    assert result["operations"] == {"describe_addresses": True}
    assert result["operation_details"]["describe_addresses"] == {
        "required": True,
        "attempts": 1,
        "status": "complete",
    }
    failed = ScanContext("test", Config.load())
    client = TrackedClient(
        Mock(describe_volumes=Mock(side_effect=RuntimeError("denied"))), failed
    )
    with pytest.raises(RuntimeError):
        client.describe_volumes()
    result = failed.result([], 0)
    assert result["discovery_complete"] is False
    assert result["operation_details"]["describe_volumes"]["status"] == "failed"


def test_inventory_covers_active_instances_and_tag_updates():
    scope = ScanContext("inventory", Config.load(), "123456789012")
    tracked = TrackedClient(Mock(), scope)
    with activate(scope):
        tracked.capture(
            "describe_instances",
            {
                "Reservations": [
                    {
                        "Instances": [
                            {"InstanceId": "active", "State": {"Name": "running"}}
                        ]
                    }
                ]
            },
        )
        tracked.capture(
            "describe_load_balancers",
            {
                "LoadBalancers": [
                    {
                        "LoadBalancerName": "lb",
                        "LoadBalancerArn": "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/lb/id",
                    }
                ]
            },
        )
        tracked.capture(
            "describe_tags",
            {
                "TagDescriptions": [
                    {
                        "ResourceArn": "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/lb/id",
                        "Tags": [{"Key": "owner", "Value": "sre"}],
                    }
                ]
            },
        )
    assert len(scope.inventory) == 2
    assert next(r for r in scope.inventory.values() if r["id"] == "lb")["tags"] == {
        "owner": "sre"
    }


def test_rds_inventory_reads_tags_for_active_nonfindings():
    raw = Mock(
        list_tags_for_resource=Mock(
            return_value={"TagList": [{"Key": "owner", "Value": "dba"}]}
        )
    )
    scope = ScanContext("rds", Config.load(), "123456789012")
    tracked = TrackedClient(raw, scope)
    with activate(scope):
        tracked.capture(
            "describe_db_instances",
            {
                "DBInstances": [
                    {
                        "DBInstanceIdentifier": "active",
                        "DBInstanceArn": "arn:aws:rds:us-east-1:123456789012:db:active",
                    }
                ]
            },
        )
    assert next(iter(scope.inventory.values()))["tags"] == {"owner": "dba"}


def test_archives_use_explicit_keys_and_kms(monkeypatch):
    s3 = Mock()
    monkeypatch.setattr(s3_archiver, "_s3_client", s3)
    monkeypatch.setenv("REPORT_BUCKET", "reports")
    monkeypatch.setenv("S3_KMS_KEY_ID", "arn:aws:kms:us-east-1:123456789012:key/key")
    monkeypatch.setenv("S3_STORAGE_CLASS", "STANDARD_IA")
    assert (
        s3_archiver.archive_report("full", key="reports/run/report.md")
        == "reports/run/report.md"
    )
    assert s3.put_object.call_args.kwargs["ServerSideEncryption"] == "aws:kms"
    s3_archiver.archive_json({"unknown": None}, "reports/run/manifest.json")
    assert json.loads(s3.put_object.call_args.kwargs["Body"]) == {"unknown": None}
    assert s3.put_object.call_args.kwargs["ContentType"] == "application/json"


def test_json_archive_reports_the_exact_encoded_size(monkeypatch):
    s3 = Mock()
    monkeypatch.setattr(s3_archiver, "_s3_client", s3)
    monkeypatch.setenv("REPORT_BUCKET", "reports")
    measured = []
    value = {"scanner": "scan_unattached_eips", "inventory": [{"id": "eipalloc-123"}]}

    s3_archiver.archive_json(
        value, "reports/run/attempt.json", on_encoded=measured.append
    )

    body = s3.put_object.call_args.kwargs["Body"]
    assert measured == [len(body)]
    assert json.loads(body) == value


def test_scanner_artifact_size_is_emitted_as_a_metric(capsys):
    from utils.logging_config import emit_scanner_metrics

    emit_scanner_metrics(
        {"scanner": "scan_unattached_eips", "status": "complete"}, 12345
    )
    metric = json.loads(capsys.readouterr().out)

    assert metric["ScannerArtifactBytes"] == 12345
    assert {"Name": "ScannerArtifactBytes", "Unit": "Bytes"} in metric["_aws"][
        "CloudWatchMetrics"
    ][0]["Metrics"]


def test_s3_idempotent_put_verifies_existing_object(monkeypatch):
    from botocore.exceptions import ClientError

    s3 = Mock()
    monkeypatch.setattr(s3_archiver, "_s3_client", s3)
    monkeypatch.setenv("REPORT_BUCKET", "reports")
    body = json.dumps({"run": "stable"}, allow_nan=False, default=str).encode()
    import hashlib

    digest = hashlib.sha256(body).hexdigest()
    s3.put_object.side_effect = ClientError(
        {"Error": {"Code": "PreconditionFailed"}}, "PutObject"
    )
    s3.head_object.return_value = {"Metadata": {"waste-hunter-sha256": digest}}
    assert (
        s3_archiver.archive_json({"run": "stable"}, "reports/run/manifest.json")
        == "reports/run/manifest.json"
    )
    assert s3.put_object.call_args.kwargs["IfNoneMatch"] == "*"
    s3.head_object.assert_called_once()


def test_s3_idempotent_key_rejects_different_content(monkeypatch):
    from botocore.exceptions import ClientError

    s3 = Mock()
    monkeypatch.setattr(s3_archiver, "_s3_client", s3)
    monkeypatch.setenv("REPORT_BUCKET", "reports")
    s3.put_object.side_effect = ClientError(
        {"Error": {"Code": "PreconditionFailed"}}, "PutObject"
    )
    s3.head_object.return_value = {"Metadata": {"waste-hunter-sha256": "different"}}
    with pytest.raises(ValueError, match="different content"):
        s3_archiver.archive_json({"run": "new"}, "reports/run/manifest.json")


def test_s3_conditional_conflict_retries_when_winner_is_not_yet_readable(monkeypatch):
    from botocore.exceptions import ClientError

    conflict = ClientError(
        {"Error": {"Code": "ConditionalRequestConflict"}}, "PutObject"
    )
    not_visible = ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
    s3 = Mock(
        put_object=Mock(side_effect=[conflict, {}]),
        head_object=Mock(side_effect=not_visible),
    )
    monkeypatch.setattr(s3_archiver, "_s3_client", s3)
    monkeypatch.setattr(s3_archiver.time, "sleep", Mock())
    monkeypatch.setenv("REPORT_BUCKET", "reports")

    assert (
        s3_archiver.archive_json({"run": "stable"}, "reports/run/manifest.json")
        == "reports/run/manifest.json"
    )
    assert s3.put_object.call_count == 2
    assert s3.head_object.call_count == 1


def test_s3_conditional_conflict_retries_are_bounded(monkeypatch):
    from botocore.exceptions import ClientError

    conflict = ClientError(
        {"Error": {"Code": "ConditionalRequestConflict"}}, "PutObject"
    )
    not_visible = ClientError({"Error": {"Code": "404"}}, "HeadObject")
    s3 = Mock(
        put_object=Mock(side_effect=conflict), head_object=Mock(side_effect=not_visible)
    )
    monkeypatch.setattr(s3_archiver, "_s3_client", s3)
    monkeypatch.setattr(s3_archiver.time, "sleep", Mock())
    monkeypatch.setenv("REPORT_BUCKET", "reports")

    with pytest.raises(ClientError) as raised:
        s3_archiver.archive_json({"run": "stable"}, "reports/run/manifest.json")
    assert raised.value.response["Error"]["Code"] == "ConditionalRequestConflict"
    assert s3.put_object.call_count == s3_archiver.S3_CONFLICT_MAX_ATTEMPTS
    assert s3.head_object.call_count == s3_archiver.S3_CONFLICT_MAX_ATTEMPTS


def test_simultaneous_s3_writers_share_the_same_idempotent_object(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from botocore.exceptions import ClientError

    class ConcurrentS3:
        def __init__(self):
            self.lock = threading.Lock()
            self.barrier = threading.Barrier(2)
            self.objects = {}
            self.puts = 0
            self.heads = 0

        def put_object(self, **params):
            self.barrier.wait(timeout=5)
            with self.lock:
                self.puts += 1
                if params["Key"] in self.objects:
                    raise ClientError(
                        {"Error": {"Code": "PreconditionFailed"}}, "PutObject"
                    )
                self.objects[params["Key"]] = params["Metadata"]

        def head_object(self, **params):
            with self.lock:
                self.heads += 1
                return {"Metadata": self.objects[params["Key"]]}

    s3 = ConcurrentS3()
    monkeypatch.setattr(s3_archiver, "_s3_client", s3)
    monkeypatch.setenv("REPORT_BUCKET", "reports")
    key = "reports/run/manifest.json"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: s3_archiver.archive_json({"run": "same"}, key), range(2))
        )

    assert results == [key, key]
    assert s3.puts == 2 and s3.heads == 1
    assert len(s3.objects) == 1


@pytest.mark.parametrize("bucket", ["arn:aws:s3:::bucket", "bucket/path"])
def test_invalid_archive_bucket_rejected(monkeypatch, bucket):
    monkeypatch.setenv("REPORT_BUCKET", bucket)
    with pytest.raises(ValueError):
        s3_archiver.archive_report("report")


def test_archive_failure_is_not_acknowledged(monkeypatch):
    monkeypatch.setenv("REPORT_BUCKET", "reports")
    monkeypatch.setattr(
        s3_archiver,
        "_s3_client",
        Mock(put_object=Mock(side_effect=RuntimeError("denied"))),
    )
    with pytest.raises(RuntimeError):
        s3_archiver.archive_report("full")


@pytest.mark.parametrize("value", [None, "bad", float("nan"), float("inf"), 10**10])
def test_invalid_sns_numeric_attributes_are_omitted(value):
    assert "monthly_waste" not in sns_sender._build_message_attributes(
        {"monthly_waste": value}
    )


def test_sns_topic_region_and_subject(monkeypatch):
    monkeypatch.setenv("SNS_TOPIC_REGION", "eu-west-1")
    assert sns_sender._topic_region() == "eu-west-1"
    monkeypatch.delenv("SNS_TOPIC_REGION")
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:reports")
    assert sns_sender._topic_region() == "us-east-1"
    monkeypatch.setenv("REPORT_SCHEDULE", "Daily\nInjected")
    sns = Mock(publish=Mock(return_value={"MessageId": "id"}))
    monkeypatch.setattr(sns_sender, "_sns_client", sns)
    sns_sender.send_report(
        "full",
        {
            "status": "ok",
            "high_waste": False,
            "compliance_percent": 12.5,
            "no_metrics": 1,
        },
    )
    assert "\n" not in sns.publish.call_args.kwargs["Subject"]


def test_sns_payload_has_stable_run_deduplication_key(monkeypatch):
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:reports")
    sns = Mock(publish=Mock(return_value={"MessageId": "id"}))
    monkeypatch.setattr(sns_sender, "_sns_client", sns)
    sns_sender.send_report("run run-123", {"run_id": "run-123", "status": "ok"})
    assert (
        sns.publish.call_args.kwargs["MessageAttributes"]["run_id"]["StringValue"]
        == "run-123"
    )


def test_sns_failure_is_visible_to_retry_caller(monkeypatch):
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:reports")
    sns = Mock(publish=Mock(side_effect=RuntimeError("publish failed")))
    monkeypatch.setattr(sns_sender, "_sns_client", sns)
    with pytest.raises(RuntimeError, match="publish failed"):
        sns_sender.send_report("report", {"run_id": "run-123", "status": "ok"})


def test_configuration_and_price_snapshot_do_not_drift(monkeypatch):
    from cost_engine import estimator

    config = Config.load()
    monkeypatch.setenv("PRICING_JSON", '{"EC2":{"t3.micro":999}}')
    with activate(ScanContext("pricing", config)):
        rows, total = estimator.estimate_monthly_waste(
            [
                {
                    "type": "EC2",
                    "id": "i",
                    "region": "us-east-1",
                    "instance_type": "t3.micro",
                }
            ]
        )
    assert total == 8.5
