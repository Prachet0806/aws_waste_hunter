import pytest
from datetime import timedelta
import scanner.ec2_scanner as ec2_scanner


class FakeEC2Paginator:
    def __init__(self, reservations_data):
        self.reservations_data = reservations_data

    def paginate(self, Filters=None):
        return self.reservations_data


class FakeEC2Client:
    def __init__(self, reservations_data):
        self.reservations_data = reservations_data

    def get_paginator(self, name):
        assert name == "describe_instances"
        return FakeEC2Paginator(self.reservations_data)


class FakeCloudwatchClient:
    """Fake for get_metric_data.

    metrics_map: instance_id -> {"cpu": [..], "net_in": [..], "net_out": [..]} or None
    """

    def __init__(self, metrics_map):
        self.metrics_map = metrics_map
        self.calls = []

    def get_metric_data(self, **kwargs):
        self.calls.append(kwargs)
        results = []
        metric_key_map = {"cpu": "cpu", "netin": "net_in", "netout": "net_out"}
        for q in kwargs["MetricDataQueries"]:
            iid = q["MetricStat"]["Metric"]["Dimensions"][0]["Value"]
            prefix = q["Id"].split("_")[0]
            metric = metric_key_map[prefix]
            if iid not in self.metrics_map or self.metrics_map[iid] is None:
                values = []
            else:
                values = self.metrics_map[iid].get(metric, [])
            if values:
                if metric == "cpu":
                    values = [values[n % len(values)] for n in range(28)]
                else:
                    values = [sum(values)/28]*28
            results.append({
                "Id": q["Id"],
                "Label": iid,
                "Timestamps": [kwargs["StartTime"]+timedelta(hours=6*n) for n in range(len(values))],
                "Values": values,
                "StatusCode": "Complete",
            })
        return {"MetricDataResults": results}


def _instance(iid, itype="t3.micro", az="us-east-1a", tags=None):
    return {
        "InstanceId": iid,
        "State": {"Name": "running"},
        "InstanceType": itype,
        "Placement": {"AvailabilityZone": az},
        "Tags": tags or [],
    }


def test_scan_idle_ec2_basic(monkeypatch):
    """Test basic EC2 scanning with idle instances."""
    reservations_data = [{"Reservations": [{"Instances": [_instance("i-1", tags=[{"Key": "owner", "Value": "sre"}])]}]}]

    fake_ec2 = FakeEC2Client(reservations_data)
    fake_cw = FakeCloudwatchClient({"i-1": {"cpu": [1.0, 1.5], "net_in": [0], "net_out": [0]}})

    monkeypatch.setattr(ec2_scanner, "_ec2_client", fake_ec2)
    monkeypatch.setattr(ec2_scanner, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("CPU_THRESHOLD", "2")
    ec2_scanner.CPU_THRESHOLD = None
    ec2_scanner.NETWORK_THRESHOLD = None

    results = ec2_scanner.scan_idle_ec2()

    assert len(results) == 1
    assert results[0]["id"] == "i-1"
    assert results[0]["instance_type"] == "t3.micro"
    assert results[0]["avg_cpu"] == 1.25
    assert results[0]["region"] == "us-east-1"
    assert results[0]["tags"]["owner"] == "sre"


def test_scan_idle_ec2_pagination(monkeypatch):
    """Test pagination with multiple pages."""
    reservations_data = [
        {"Reservations": [{"Instances": [_instance("i-1")]}]},
        {"Reservations": [{"Instances": [_instance("i-2", az="us-west-2b")]}]},
    ]

    fake_ec2 = FakeEC2Client(reservations_data)
    fake_cw = FakeCloudwatchClient({
        "i-1": {"cpu": [1.0], "net_in": [0], "net_out": [0]},
        "i-2": {"cpu": [1.5], "net_in": [0], "net_out": [0]},
    })

    monkeypatch.setattr(ec2_scanner, "_ec2_client", fake_ec2)
    monkeypatch.setattr(ec2_scanner, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("CPU_THRESHOLD", "2")
    ec2_scanner.CPU_THRESHOLD = None
    ec2_scanner.NETWORK_THRESHOLD = None

    results = ec2_scanner.scan_idle_ec2()

    assert len(results) == 2
    assert results[0]["region"] == "us-east-1"
    assert results[1]["region"] == "us-west-2"


def test_scan_idle_ec2_filters_busy_instances(monkeypatch):
    """Test that busy instances are filtered out."""
    reservations_data = [{"Reservations": [{"Instances": [_instance("i-idle"), _instance("i-busy", itype="t3.large")]}]}]

    fake_ec2 = FakeEC2Client(reservations_data)
    fake_cw = FakeCloudwatchClient({
        "i-idle": {"cpu": [1.0], "net_in": [0], "net_out": [0]},
        "i-busy": {"cpu": [95.0], "net_in": [1000], "net_out": [1000]},
    })

    monkeypatch.setattr(ec2_scanner, "_ec2_client", fake_ec2)
    monkeypatch.setattr(ec2_scanner, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("CPU_THRESHOLD", "2")
    ec2_scanner.CPU_THRESHOLD = None
    ec2_scanner.NETWORK_THRESHOLD = None

    results = ec2_scanner.scan_idle_ec2()

    assert len(results) == 1
    assert results[0]["id"] == "i-idle"


def test_scan_idle_ec2_filters_network_active(monkeypatch):
    """Test that low-CPU instances with network activity are not flagged (B1)."""
    reservations_data = [{"Reservations": [{"Instances": [_instance("i-proxy")]}]}]

    fake_ec2 = FakeEC2Client(reservations_data)
    fake_cw = FakeCloudwatchClient({
        "i-proxy": {"cpu": [0.5], "net_in": [500000000], "net_out": [500000000]},
    })

    monkeypatch.setattr(ec2_scanner, "_ec2_client", fake_ec2)
    monkeypatch.setattr(ec2_scanner, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("CPU_THRESHOLD", "2")
    monkeypatch.setenv("NETWORK_THRESHOLD", "1000000")
    ec2_scanner.CPU_THRESHOLD = None
    ec2_scanner.NETWORK_THRESHOLD = None

    results = ec2_scanner.scan_idle_ec2()

    assert len(results) == 0


def test_scan_idle_ec2_skips_asg_managed(monkeypatch):
    """Test that ASG-managed instances are skipped (B2)."""
    asg_tags = [{"Key": "aws:autoscaling:groupName", "Value": "web-asg"}]
    reservations_data = [{"Reservations": [{"Instances": [_instance("i-asg", tags=asg_tags), _instance("i-standalone")]}]}]

    fake_ec2 = FakeEC2Client(reservations_data)
    fake_cw = FakeCloudwatchClient({
        "i-asg": {"cpu": [1.0], "net_in": [0], "net_out": [0]},
        "i-standalone": {"cpu": [1.0], "net_in": [0], "net_out": [0]},
    })

    monkeypatch.setattr(ec2_scanner, "_ec2_client", fake_ec2)
    monkeypatch.setattr(ec2_scanner, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("CPU_THRESHOLD", "2")
    ec2_scanner.CPU_THRESHOLD = None
    ec2_scanner.NETWORK_THRESHOLD = None

    results = ec2_scanner.scan_idle_ec2()

    assert [r["id"] for r in results] == ["i-standalone"]
    # ASG instance should never have been queried
    queried_ids = set()
    for call in fake_cw.calls:
        for q in call["MetricDataQueries"]:
            queried_ids.add(q["MetricStat"]["Metric"]["Dimensions"][0]["Value"])
    assert "i-asg" not in queried_ids


def test_scan_idle_ec2_metric_batching(monkeypatch):
    """Test GetMetricData batching across 500-query limit (C1)."""
    n = 200  # 200 instances * 3 queries = 600 -> must be split into 2 calls
    instances = [_instance(f"i-{i}") for i in range(n)]
    reservations_data = [{"Reservations": [{"Instances": instances}]}]

    metrics_map = {f"i-{i}": {"cpu": [1.0], "net_in": [0], "net_out": [0]} for i in range(n)}
    fake_ec2 = FakeEC2Client(reservations_data)
    fake_cw = FakeCloudwatchClient(metrics_map)

    monkeypatch.setattr(ec2_scanner, "_ec2_client", fake_ec2)
    monkeypatch.setattr(ec2_scanner, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("CPU_THRESHOLD", "2")
    ec2_scanner.CPU_THRESHOLD = None
    ec2_scanner.NETWORK_THRESHOLD = None

    results = ec2_scanner.scan_idle_ec2()

    assert len(results) == n
    assert len(fake_cw.calls) == 2
    assert len(fake_cw.calls[0]["MetricDataQueries"]) == 498
    assert len(fake_cw.calls[1]["MetricDataQueries"]) == 102


def test_scan_idle_ec2_no_metrics(monkeypatch):
    """Test handling of instances with no CloudWatch metrics."""
    reservations_data = [{"Reservations": [{"Instances": [_instance("i-new")]}]}]

    fake_ec2 = FakeEC2Client(reservations_data)
    fake_cw = FakeCloudwatchClient({"i-new": None})

    monkeypatch.setattr(ec2_scanner, "_ec2_client", fake_ec2)
    monkeypatch.setattr(ec2_scanner, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("CPU_THRESHOLD", "2")
    ec2_scanner.CPU_THRESHOLD = None
    ec2_scanner.NETWORK_THRESHOLD = None

    results = ec2_scanner.scan_idle_ec2()

    assert len(results) == 0
    assert ec2_scanner.get_no_metrics_count() == 1


def test_get_no_metrics_count_reset(monkeypatch):
    """Test that no_metrics count resets between scans."""
    reservations_data = [{"Reservations": [{"Instances": [_instance("i-1")]}]}]

    fake_ec2 = FakeEC2Client(reservations_data)
    fake_cw = FakeCloudwatchClient({"i-1": {"cpu": [1.0], "net_in": [0], "net_out": [0]}})

    monkeypatch.setattr(ec2_scanner, "_ec2_client", fake_ec2)
    monkeypatch.setattr(ec2_scanner, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("CPU_THRESHOLD", "2")
    ec2_scanner.CPU_THRESHOLD = None
    ec2_scanner.NETWORK_THRESHOLD = None

    results = ec2_scanner.scan_idle_ec2()

    assert len(results) == 1
    assert ec2_scanner.get_no_metrics_count() == 0


def test_scan_idle_ec2_custom_threshold(monkeypatch):
    """Test custom CPU threshold from env var."""
    reservations_data = [{"Reservations": [{"Instances": [_instance("i-1")]}]}]

    fake_ec2 = FakeEC2Client(reservations_data)
    fake_cw = FakeCloudwatchClient({"i-1": {"cpu": [3.0], "net_in": [0], "net_out": [0]}})

    monkeypatch.setattr(ec2_scanner, "_ec2_client", fake_ec2)
    monkeypatch.setattr(ec2_scanner, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("CPU_THRESHOLD", "5")
    ec2_scanner.CPU_THRESHOLD = None
    ec2_scanner.NETWORK_THRESHOLD = None

    results = ec2_scanner.scan_idle_ec2()

    assert len(results) == 1  # 3% < 5% threshold


def test_scan_idle_ec2_invalid_threshold(monkeypatch):
    """Test handling of invalid CPU threshold."""
    reservations_data = [{"Reservations": [{"Instances": [_instance("i-1")]}]}]

    fake_ec2 = FakeEC2Client(reservations_data)
    fake_cw = FakeCloudwatchClient({"i-1": {"cpu": [1.0], "net_in": [0], "net_out": [0]}})

    monkeypatch.setattr(ec2_scanner, "_ec2_client", fake_ec2)
    monkeypatch.setattr(ec2_scanner, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("CPU_THRESHOLD", "invalid")
    ec2_scanner.CPU_THRESHOLD = None
    ec2_scanner.NETWORK_THRESHOLD = None

    with pytest.raises(ValueError):
        ec2_scanner.scan_idle_ec2()


def test_scan_idle_ec2_custom_network_threshold(monkeypatch):
    """Test custom network threshold from env var."""
    reservations_data = [{"Reservations": [{"Instances": [_instance("i-1")]}]}]

    fake_ec2 = FakeEC2Client(reservations_data)
    # 800 bytes total, threshold set to 1000 -> idle; default would allow 1MB anyway
    fake_cw = FakeCloudwatchClient({"i-1": {"cpu": [1.0], "net_in": [400], "net_out": [400]}})

    monkeypatch.setattr(ec2_scanner, "_ec2_client", fake_ec2)
    monkeypatch.setattr(ec2_scanner, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("CPU_THRESHOLD", "2")
    monkeypatch.setenv("NETWORK_THRESHOLD", "1000")
    ec2_scanner.CPU_THRESHOLD = None
    ec2_scanner.NETWORK_THRESHOLD = None

    results = ec2_scanner.scan_idle_ec2()

    assert len(results) == 1
    assert results[0]["net_in_bytes"] == 400.0


def test_scan_idle_ec2_local_zone(monkeypatch):
    """Test region extraction from Local Zone."""
    reservations_data = [{"Reservations": [{"Instances": [_instance("i-1", az="us-east-1-bos-1a")]}]}]

    fake_ec2 = FakeEC2Client(reservations_data)
    fake_cw = FakeCloudwatchClient({"i-1": {"cpu": [1.0], "net_in": [0], "net_out": [0]}})

    monkeypatch.setattr(ec2_scanner, "_ec2_client", fake_ec2)
    monkeypatch.setattr(ec2_scanner, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("CPU_THRESHOLD", "2")
    ec2_scanner.CPU_THRESHOLD = None
    ec2_scanner.NETWORK_THRESHOLD = None

    results = ec2_scanner.scan_idle_ec2()

    assert len(results) == 1
    assert results[0]["az"] == "us-east-1-bos-1a"
    assert results[0]["region"] == "us-east-1"