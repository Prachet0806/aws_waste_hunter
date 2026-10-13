"""Regression evidence for the audit defects; no network or AWS mutations."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from utils.config import Config
from utils.scan_context import ScanContext, activate
from utils.metrics import collect, statistics
from utils.schema import resource_identity, build_resource
import lambda_handler as handler
from cost_engine import estimator
from compliance.tag_checker import check_tag_compliance, compute_compliance_stats


START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = START+timedelta(days=7)


def metric(key, values, offset=0, status="Complete"):
    return {"Id": key, "Values": values,
            "Timestamps": [START+timedelta(hours=6*(n+offset)) for n in range(len(values))],
            "StatusCode": status}


@pytest.mark.parametrize("name,value", [
    ("CPU_THRESHOLD", "nan"), ("RDS_CPU_THRESHOLD", "101"), ("NETWORK_THRESHOLD", "-1"),
    ("MIN_METRIC_COVERAGE", "0.1"), ("MAX_SCANNER_WORKERS", "2.5"),
    ("SCAN_TIMEOUT_SECONDS", "inf"), ("PRICING_MODE", "bogus"), ("EXCLUDED_TAGS_JSON", "[]"),
    ("S3_SSE", "invalid"), ("S3_SSE", "aws:kms")])
def test_invalid_configuration_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Config.load()


def test_ec2_pages_preserve_busy_history():
    from scanner.ec2_scanner import _fetch_instance_metrics
    cw = Mock()
    cw.get_metric_data.side_effect = [
        {"MetricDataResults": [metric("cpu_0", [80]*14, status="PartialData"),
                               metric("netin_0", [1000000]*14), metric("netout_0", [0]*14)], "NextToken": "more"},
        {"MetricDataResults": [metric("cpu_0", [0]*14, 14), metric("netin_0", [0]*14, 14), metric("netout_0", [0]*14, 14)]}]
    result = _fetch_instance_metrics(cw, ["i-active"], START, END)["i-active"]
    assert result["cpu"] == 40
    assert result["net_in"] == 14000000


def test_rds_pages_preserve_peak_connections():
    from scanner.rds_idle_scanner import _fetch_rds_metrics
    cw = Mock()
    cw.get_metric_data.side_effect = [
        {"MetricDataResults": [metric("cpuutilization_0", [.5]*14), metric("databaseconnections_0", [100]*14)], "NextToken": "more"},
        {"MetricDataResults": [metric("cpuutilization_0", [.5]*14, 14), metric("databaseconnections_0", [0]*14, 14)]}]
    assert _fetch_rds_metrics(cw, ["db"], ["CPUUtilization", "DatabaseConnections"], START, END)["db"]["connections"] == 100


@pytest.mark.parametrize("response", [
    {"MetricDataResults": [metric("cpu", [0])]},
    {"MetricDataResults": [metric("cpu", [0]*28, status="PartialData")]},
    {"MetricDataResults": [metric("cpu", [float("nan")]*28)]},
    {"MetricDataResults": [{"Id": "cpu", "Values": [0], "Timestamps": [], "StatusCode": "Complete"}]},
    {"MetricDataResults": []}])
def test_incomplete_metrics_never_prove_zero(response):
    cw = Mock(get_metric_data=Mock(return_value=response))
    assert collect(cw, [{"Id": "cpu"}], START, END, 21600)["cpu"]["valid"] is False


def test_missing_network_not_filled_with_zero():
    from scanner.ec2_scanner import _fetch_instance_metrics
    cw = Mock(get_metric_data=Mock(return_value={"MetricDataResults": [metric("cpu_0", [.5]*28)]}))
    result = _fetch_instance_metrics(cw, ["i"], START, END)["i"]
    assert result["cpu"] is None and result["net_in"] is None


def test_metric_page_failure_preserves_error_not_classification():
    scope = ScanContext("test", Config.load())
    cw = Mock(get_metric_data=Mock(side_effect=RuntimeError("denied")))
    with activate(scope):
        result = collect(cw, [{"Id": "cpu"}], START, END, 21600)
    assert not result["cpu"]["valid"] and scope.errors


def test_repeated_pagination_token_stops():
    cw = Mock(get_metric_data=Mock(return_value={"MetricDataResults": [], "NextToken": "same"}))
    assert not collect(cw, [{"Id": "cpu"}], START, END, 21600)["cpu"]["valid"]
    assert cw.get_metric_data.call_count == 2


def test_local_deadline_does_not_wait_for_worker_shutdown():
    started = time.monotonic()
    _, errors = handler._run_scanners_parallel([("slow", lambda: (time.sleep(.2) or []))], 1, timeout=.01)
    assert time.monotonic()-started < .15
    assert errors[0]["type"] == "TimeoutError"


def test_contexts_do_not_reset_other_scanners():
    from utils.aws_helpers import log_and_skip, reset_no_metrics
    left, right = ScanContext("left", Config.load()), ScanContext("right", Config.load())
    with activate(left):
        log_and_skip("a", "missing metrics")
        with activate(right):
            reset_no_metrics()
            log_and_skip("b", "permission denied")
    assert left.skips[0]["resource_id"] == "a" and right.skips[0]["resource_id"] == "b"


def test_snapshot_reference_failure_cannot_create_candidates(monkeypatch):
    from scanner import snapshot_scanner as scanner
    client = Mock()
    client.get_paginator.side_effect = RuntimeError("AccessDenied")
    monkeypatch.setattr(scanner, "_ec2_client", client)
    with pytest.raises(RuntimeError, match="AccessDenied"):
        scanner.scan_orphaned_snapshots()
    assert client.get_paginator.call_count == 1


@pytest.mark.parametrize("tags", [[{"Key": "waste-hunter-backup", "Value": "true"}],
                                  [{"Key": "aws:backup:source-resource", "Value": "arn"}]])
def test_backup_snapshots_are_protected(monkeypatch, tags):
    from scanner import snapshot_scanner as scanner
    client = Mock(meta=SimpleNamespace(region_name="us-east-1"))
    client.get_paginator.side_effect = [Mock(paginate=Mock(return_value=[{"Images": []}])),
        Mock(paginate=Mock(return_value=[{"Snapshots": [{"SnapshotId": "snap", "Tags": tags,
            "StartTime": START, "State": "completed"}]}]))]
    monkeypatch.setattr(scanner, "_ec2_client", client)
    assert scanner.scan_orphaned_snapshots() == []
    client.describe_snapshot_attribute.assert_not_called()


def test_shared_snapshot_is_excluded(monkeypatch):
    from scanner import snapshot_scanner as scanner
    client = Mock(meta=SimpleNamespace(region_name="us-east-1"))
    client.get_paginator.side_effect = [Mock(paginate=Mock(return_value=[{"Images": []}])),
        Mock(paginate=Mock(return_value=[{"Snapshots": [{"SnapshotId": "snap", "StartTime": START, "State": "completed"}]}]))]
    client.describe_snapshot_attribute.return_value = {"CreateVolumePermissions": [{"UserId": "other"}]}
    monkeypatch.setattr(scanner, "_ec2_client", client)
    assert scanner.scan_orphaned_snapshots() == []


def test_multiattach_running_member_prevents_waste(monkeypatch):
    from scanner import ebs_scanner as scanner
    client = Mock()
    client.get_paginator.side_effect = [Mock(paginate=Mock(return_value=[{"Reservations": [{"Instances": [
        {"InstanceId": "stopped", "State": {"Name": "stopped"}, "BlockDeviceMappings": [{"Ebs": {"VolumeId": "v"}}]}]}]}])),
        Mock(paginate=Mock(return_value=[{"Volumes": [{"VolumeId": "v", "State": "in-use", "Attachments": [
            {"InstanceId": "stopped"}, {"InstanceId": "running"}]}]}]))]
    monkeypatch.setattr(scanner, "_ec2_client", client)
    assert scanner.scan_unattached_ebs() == []


def test_identity_collision_and_unknown_tags():
    resources = [build_resource("RDS_INSTANCE", "same", region="us-east-1"),
                 build_resource("RDS_CLUSTER", "same", region="us-east-1")]
    assert resource_identity(resources[0]) != resource_identity(resources[1])
    assert compute_compliance_stats(resources)["percent"] == 0
    resources[0]["tags_status"] = "unknown"
    stats = compute_compliance_stats(resources)
    assert stats["assessed"] == 1 and stats["unassessed"] == 1
    assert len(check_tag_compliance(resources)) == 1


@pytest.mark.parametrize("value", [-1, "NaN", "Infinity", None, [], "bad"])
def test_invalid_price_overrides_cannot_contaminate_total(monkeypatch, value):
    monkeypatch.setenv("PRICING_JSON", json.dumps({"EBS": value}))
    with pytest.raises((ValueError, TypeError)):
        estimator.estimate_monthly_waste([{"type": "EBS", "id": "v", "region": "us-east-1", "size_gb": 100}])


def test_rds_stopped_compute_excluded_and_storage_explicit(monkeypatch):
    rows, total = estimator.estimate_monthly_waste([{"type": "RDS_INSTANCE", "id": "db", "region": "us-east-1",
        "reason": "stopped", "instance_class": "db.r5.large"}])
    assert total == 0 and rows[0]["monthly_cost"] is None
    monkeypatch.setenv("PRICING_JSON", '{"RDS_STORAGE":{"gp2":0.1}}')
    rows, total = estimator.estimate_monthly_waste([{"type": "RDS_INSTANCE", "id": "db", "region": "us-east-1",
        "reason": "stopped", "storage_gb": 20, "storage_type": "gp2"}])
    assert total == 2 and not rows[0]["pricing"]["complete"]


def test_rds_engines_have_distinct_cache_entries(monkeypatch):
    fn = Mock(side_effect=[10, 20])
    monkeypatch.setattr(estimator, "_get_rds_monthly_price", fn)
    assert estimator.get_price("RDS_INSTANCE", "us-east-1", "live", instance_class="db.t3.micro", engine="mysql") == 10
    assert estimator.get_price("RDS_INSTANCE", "us-east-1", "live", instance_class="db.t3.micro", engine="postgres") == 20
    assert fn.call_count == 2


def test_price_outage_suppresses_repeated_identical_calls(monkeypatch):
    fn = Mock(return_value=None)
    monkeypatch.setattr(estimator, "_get_ec2_hourly_price", fn)
    estimator.clear_price_cache()
    for _ in range(3):
        estimator.get_price("EC2", "us-east-1", "live", instance_type="t3.micro")
    assert fn.call_count == 1


def test_complete_archive_and_sns_payload_boundary(monkeypatch):
    from reporting.report_builder import build_report
    from delivery import sns_sender
    report = build_report([{"type": "EC2", "id": f"i-{n}", "monthly_cost": 1} for n in range(601)], 601, [])
    assert "**EC2 i-600**" in report and "more rows in full S3" not in report
    sns = Mock(publish=Mock(return_value={"MessageId": "ok"}))
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:reports")
    monkeypatch.setattr(sns_sender, "_sns_client", sns)
    sns_sender.send_report("é"*262144, {"status": "partial", "monthly_waste": 10, "resources": 601})
    payload = sns.publish.call_args.kwargs
    attributes = sum(len(k.encode())+len(v['DataType'].encode())+len(v['StringValue'].encode()) for k,v in payload['MessageAttributes'].items())
    assert len(payload['Message'].encode())+attributes < sns_sender.SNS_MAX_BYTES


def test_all_scanners_failed_and_no_report_are_failed(monkeypatch):
    monkeypatch.setattr(handler, "send_report", Mock())
    monkeypatch.setattr(handler, "archive_report", Mock(return_value="key"))
    results = [{"scanner": "all", "status": "failed", "findings": [], "inventory": [], "errors": []}]
    result = handler.finalize(results, Config.load(), "123456789012", "run", durable=False)
    assert result['status'] == 'failed' and result['monthly_waste'] is None
    monkeypatch.setattr(handler, "build_report", Mock(side_effect=RuntimeError('cannot render')))
    results[0]['status'] = 'complete'
    assert handler.finalize(results, Config.load(), "123456789012", "run", durable=False)['status'] == 'failed'


def test_s3_failure_notification_has_no_false_archive(monkeypatch):
    sent = Mock()
    monkeypatch.setattr(handler, "send_report", sent)
    monkeypatch.setattr(handler, "archive_report", Mock(side_effect=RuntimeError('denied')))
    result = handler.finalize([{"scanner": "empty", "status": "complete", "findings": [], "inventory": [], "errors": []}],
                              Config.load(), "123456789012", "run", durable=False)
    assert result['status'] == 'partial'
    assert 'Archive unavailable' in sent.call_args.args[0]
    assert sent.call_args.args[1]['delivery_errors'] == 1


def test_event_logging_never_includes_nested_credentials():
    text = handler._summarize_event({'id':'event','secret':'hidden','detail':{'password':'hidden'}})
    assert 'event' in text and 'hidden' not in text


def test_sparse_statistics_cannot_claim_zero():
    assert not statistics({'Datapoints':[]}, START, END)['complete']
    assert not statistics({'Datapoints':[{'Timestamp':START,'Sum':0}]}, START, END)['complete']
    assert statistics({'Datapoints':[{'Timestamp':START,'Sum':10}]}, START, END)['total'] == 10


def test_swallowed_discovery_errors_are_failed_without_inventory():
    from utils.scan_context import record_error
    def swallowed():
        record_error('discovery', RuntimeError('denied'))
        return []
    result = handler.run_scanner('denied', swallowed, Config.load(), '123456789012', float('inf'))
    assert result['status'] == 'failed' and not result['discovery_complete']


def test_unknown_alert_attribute_is_not_false():
    from delivery.sns_sender import _build_message_attributes
    assert 'high_waste' not in _build_message_attributes({'high_waste':None})


def test_failed_scan_report_does_not_claim_zero_money():
    from reporting.report_builder import build_report
    text = build_report([], None, [])
    assert 'Unavailable' in text
    assert 'Subtotal (approximate list prices):** $0.00' not in text


def test_exact_deployment_override_supports_aurora_without_overlap(monkeypatch):
    monkeypatch.setenv('PRICING_JSON', '{"RDS_CONFIGS":{"us-east-1|aurora-mysql|db.r5.large|Single-AZ":200}}')
    rows,total = estimator.estimate_monthly_waste([
        {'type':'RDS_INSTANCE','id':'writer','region':'us-east-1','engine':'aurora-mysql','instance_class':'db.r5.large'},
        {'type':'RDS_CLUSTER','id':'cluster','region':'us-east-1','engine':'aurora-mysql'}])
    assert total == 200
    assert rows[0]['pricing']['source'] == 'configured-deployment-rate'
    assert rows[1]['monthly_cost'] is None
