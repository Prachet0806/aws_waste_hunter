import scanner.ec2_scanner as ec2_scanner
import os


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
    def __init__(self, metrics_map):
        self.metrics_map = metrics_map
        self.calls = []

    def get_metric_statistics(self, **kwargs):
        iid = kwargs["Dimensions"][0]["Value"]
        self.calls.append(iid)
        
        if iid not in self.metrics_map:
            return {"Datapoints": []}
        
        datapoints = self.metrics_map[iid]
        if datapoints is None:
            return {"Datapoints": []}
        
        return {"Datapoints": [{"Average": avg} for avg in datapoints]}


def test_scan_idle_ec2_basic(monkeypatch):
    """Test basic EC2 scanning with idle instances."""
    reservations_data = [
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-1",
                            "InstanceType": "t3.micro",
                            "Placement": {"AvailabilityZone": "us-east-1a"},
                            "Tags": [{"Key": "owner", "Value": "sre"}]
                        }
                    ]
                }
            ]
        }
    ]
    
    fake_ec2 = FakeEC2Client(reservations_data)
    fake_cw = FakeCloudwatchClient({"i-1": [1.0, 1.5]})
    
    monkeypatch.setattr(ec2_scanner, "_ec2_client", fake_ec2)
    monkeypatch.setattr(ec2_scanner, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("CPU_THRESHOLD", "2")
    ec2_scanner.CPU_THRESHOLD = None  # Reset cached value

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
        {
            "Reservations": [
                {"Instances": [
                    {"InstanceId": "i-1", "InstanceType": "t3.micro", "Placement": {"AvailabilityZone": "us-east-1a"}, "Tags": []}
                ]}
            ]
        },
        {
            "Reservations": [
                {"Instances": [
                    {"InstanceId": "i-2", "InstanceType": "t3.small", "Placement": {"AvailabilityZone": "us-west-2b"}, "Tags": []}
                ]}
            ]
        }
    ]
    
    fake_ec2 = FakeEC2Client(reservations_data)
    fake_cw = FakeCloudwatchClient({"i-1": [1.0], "i-2": [1.5]})
    
    monkeypatch.setattr(ec2_scanner, "_ec2_client", fake_ec2)
    monkeypatch.setattr(ec2_scanner, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("CPU_THRESHOLD", "2")
    ec2_scanner.CPU_THRESHOLD = None

    results = ec2_scanner.scan_idle_ec2()

    assert len(results) == 2
    assert results[0]["region"] == "us-east-1"
    assert results[1]["region"] == "us-west-2"


def test_scan_idle_ec2_filters_busy_instances(monkeypatch):
    """Test that busy instances are filtered out."""
    reservations_data = [
        {
            "Reservations": [
                {"Instances": [
                    {"InstanceId": "i-idle", "InstanceType": "t3.micro", "Placement": {"AvailabilityZone": "us-east-1a"}, "Tags": []},
                    {"InstanceId": "i-busy", "InstanceType": "t3.large", "Placement": {"AvailabilityZone": "us-east-1a"}, "Tags": []}
                ]}
            ]
        }
    ]
    
    fake_ec2 = FakeEC2Client(reservations_data)
    fake_cw = FakeCloudwatchClient({"i-idle": [1.0], "i-busy": [95.0]})
    
    monkeypatch.setattr(ec2_scanner, "_ec2_client", fake_ec2)
    monkeypatch.setattr(ec2_scanner, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("CPU_THRESHOLD", "2")
    ec2_scanner.CPU_THRESHOLD = None

    results = ec2_scanner.scan_idle_ec2()

    assert len(results) == 1
    assert results[0]["id"] == "i-idle"


def test_scan_idle_ec2_no_metrics(monkeypatch):
    """Test handling of instances with no CloudWatch metrics."""
    reservations_data = [
        {
            "Reservations": [
                {"Instances": [
                    {"InstanceId": "i-new", "InstanceType": "t3.micro", "Placement": {"AvailabilityZone": "us-east-1a"}, "Tags": []}
                ]}
            ]
        }
    ]
    
    fake_ec2 = FakeEC2Client(reservations_data)
    fake_cw = FakeCloudwatchClient({"i-new": None})  # No metrics
    
    monkeypatch.setattr(ec2_scanner, "_ec2_client", fake_ec2)
    monkeypatch.setattr(ec2_scanner, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("CPU_THRESHOLD", "2")
    ec2_scanner.CPU_THRESHOLD = None

    results = ec2_scanner.scan_idle_ec2()

    assert len(results) == 0  # Should skip instances with no metrics


def test_scan_idle_ec2_custom_threshold(monkeypatch):
    """Test custom CPU threshold from env var."""
    reservations_data = [
        {
            "Reservations": [
                {"Instances": [
                    {"InstanceId": "i-1", "InstanceType": "t3.micro", "Placement": {"AvailabilityZone": "us-east-1a"}, "Tags": []}
                ]}
            ]
        }
    ]
    
    fake_ec2 = FakeEC2Client(reservations_data)
    fake_cw = FakeCloudwatchClient({"i-1": [3.0]})
    
    monkeypatch.setattr(ec2_scanner, "_ec2_client", fake_ec2)
    monkeypatch.setattr(ec2_scanner, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("CPU_THRESHOLD", "5")
    ec2_scanner.CPU_THRESHOLD = None

    results = ec2_scanner.scan_idle_ec2()

    assert len(results) == 1  # 3% < 5% threshold


def test_scan_idle_ec2_invalid_threshold(monkeypatch):
    """Test handling of invalid CPU threshold."""
    reservations_data = [
        {
            "Reservations": [
                {"Instances": [
                    {"InstanceId": "i-1", "InstanceType": "t3.micro", "Placement": {"AvailabilityZone": "us-east-1a"}, "Tags": []}
                ]}
            ]
        }
    ]
    
    fake_ec2 = FakeEC2Client(reservations_data)
    fake_cw = FakeCloudwatchClient({"i-1": [1.0]})
    
    monkeypatch.setattr(ec2_scanner, "_ec2_client", fake_ec2)
    monkeypatch.setattr(ec2_scanner, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("CPU_THRESHOLD", "invalid")
    ec2_scanner.CPU_THRESHOLD = None

    results = ec2_scanner.scan_idle_ec2()

    # Should use default threshold of 2
    assert len(results) == 1


def test_scan_idle_ec2_local_zone(monkeypatch):
    """Test region extraction from Local Zone."""
    reservations_data = [
        {
            "Reservations": [
                {"Instances": [
                    {"InstanceId": "i-1", "InstanceType": "t3.micro", "Placement": {"AvailabilityZone": "us-east-1-bos-1a"}, "Tags": []}
                ]}
            ]
        }
    ]
    
    fake_ec2 = FakeEC2Client(reservations_data)
    fake_cw = FakeCloudwatchClient({"i-1": [1.0]})
    
    monkeypatch.setattr(ec2_scanner, "_ec2_client", fake_ec2)
    monkeypatch.setattr(ec2_scanner, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("CPU_THRESHOLD", "2")
    ec2_scanner.CPU_THRESHOLD = None

    results = ec2_scanner.scan_idle_ec2()

    assert len(results) == 1
    assert results[0]["az"] == "us-east-1-bos-1a"
    assert results[0]["region"] == "us-east-1"
