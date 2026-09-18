from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
import scanner.rds_idle_scanner as rds_idle


class FakeRDSPaginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kwargs):
        return self.pages


class FakeRDSClient:
    def __init__(self, instances_pages=None, clusters_pages=None):
        self._region = "us-east-1"
        self.meta = SimpleNamespace(region_name=self._region)
        self.instances_pages = instances_pages or [{"DBInstances": []}]
        self.clusters_pages = clusters_pages or [{"DBClusters": []}]

    def get_paginator(self, name):
        if name == "describe_db_instances":
            return FakeRDSPaginator(self.instances_pages)
        if name == "describe_db_clusters":
            return FakeRDSPaginator(self.clusters_pages)
        raise AssertionError(f"unexpected paginator {name}")

    def list_tags_for_resource(self, ResourceName):
        return {"TagList": []}


class FakeCloudwatchClient:
    def __init__(self, metrics_map):
        self.metrics_map = metrics_map
        self.calls = []

    def get_metric_data(self, **kwargs):
        self.calls.append(kwargs)
        results = []
        for q in kwargs["MetricDataQueries"]:
            # Get resource ID from either instance or cluster dimensions
            dims = q["MetricStat"]["Metric"]["Dimensions"]
            rid = None
            for d in dims:
                if d["Name"] in ("DBInstanceIdentifier", "DBClusterIdentifier"):
                    rid = d["Value"]
                    break
            if not rid:
                rid = "unknown"

            parts = q["Id"].split("_", 1)
            metric = parts[0]
            
            if rid not in self.metrics_map or self.metrics_map[rid] is None:
                values = []
            else:
                values = self.metrics_map[rid].get(metric, [])
            results.append({
                "Id": q["Id"],
                "Label": rid,
                "Timestamps": [],
                "Values": values,
                "StatusCode": "Complete",
            })
        return {"MetricDataResults": results}


def _instance(instance_id, status="available", instance_class="db.t3.micro", az="us-east-1a", engine="mysql"):
    return {
        "DBInstanceIdentifier": instance_id,
        "DBInstanceStatus": status,
        "DBInstanceClass": instance_class,
        "AvailabilityZone": az,
        "Engine": engine,
        "DBInstanceArn": f"arn:aws:rds:us-east-1:123456789012:db:{instance_id}",
    }


def _cluster(cluster_id, status="available", az_list=None, engine="aurora-mysql"):
    return {
        "DBClusterIdentifier": cluster_id,
        "Status": status,
        "AvailabilityZones": az_list or ["us-east-1a"],
        "Engine": engine,
        "DBClusterArn": f"arn:aws:rds:us-east-1:123456789012:cluster:{cluster_id}",
    }


def test_scan_idle_rds_instance_basic(monkeypatch):
    """Test basic RDS idle instance detection."""
    instances = [{
        "DBInstances": [
            _instance("db-idle"),
        ]
    }]

    fake_rds = FakeRDSClient(instances_pages=instances)
    fake_cw = FakeCloudwatchClient({"db-idle": {"cpuutilization": [2.0], "databaseconnections": [0]}})

    # Mock list_tags_for_resource
    def mock_list_tags(ResourceName):
        if "db-idle" in ResourceName:
            return {"TagList": [{"Key": "owner", "Value": "dba"}]}
        return {"TagList": []}
    fake_rds.list_tags_for_resource = mock_list_tags

    monkeypatch.setattr(rds_idle, "_rds_client", fake_rds)
    monkeypatch.setattr(rds_idle, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("RDS_CPU_THRESHOLD", "5")
    monkeypatch.setenv("RDS_CONNECTIONS_THRESHOLD", "1")
    rds_idle.CPU_THRESHOLD = None
    rds_idle.CONNECTIONS_THRESHOLD = None

    results = rds_idle.scan_idle_rds()

    assert len(results) == 1
    assert results[0]["type"] == "RDS_INSTANCE"
    assert results[0]["id"] == "db-idle"
    assert results[0]["instance_class"] == "db.t3.micro"
    assert results[0]["avg_cpu"] == 2.0
    assert results[0]["avg_connections"] == 0
    assert results[0]["region"] == "us-east-1"
    assert results[0]["tags"]["owner"] == "dba"


def test_scan_idle_rds_cluster_basic(monkeypatch):
    """Test basic RDS idle cluster detection."""
    clusters = [{
        "DBClusters": [
            _cluster("cluster-idle"),
        ]
    }]

    fake_rds = FakeRDSClient(clusters_pages=clusters)
    fake_cw = FakeCloudwatchClient({"cluster-idle": {"cpuutilization": [3.0], "databaseconnections": [1]}})

    monkeypatch.setattr(rds_idle, "_rds_client", fake_rds)
    monkeypatch.setattr(rds_idle, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("RDS_CPU_THRESHOLD", "5")
    monkeypatch.setenv("RDS_CONNECTIONS_THRESHOLD", "1")
    rds_idle.CPU_THRESHOLD = None
    rds_idle.CONNECTIONS_THRESHOLD = None

    results = rds_idle.scan_idle_rds()

    assert len(results) == 1
    assert results[0]["type"] == "RDS_CLUSTER"
    assert results[0]["id"] == "cluster-idle"
    assert results[0]["avg_cpu"] == 3.0
    assert results[0]["avg_connections"] == 1


def test_scan_idle_rds_skips_busy_instance(monkeypatch):
    """Test that instances with high CPU/connections are not flagged."""
    instances = [{
        "DBInstances": [
            _instance("db-busy"),
            _instance("db-idle"),
        ]
    }]

    fake_rds = FakeRDSClient(instances_pages=instances)
    fake_cw = FakeCloudwatchClient({
        "db-busy": {"cpuutilization": [80.0], "databaseconnections": [100]},
        "db-idle": {"cpuutilization": [2.0], "databaseconnections": [0]},
    })

    def mock_list_tags(ResourceName):
        return {"TagList": []}
    fake_rds.list_tags_for_resource = mock_list_tags

    monkeypatch.setattr(rds_idle, "_rds_client", fake_rds)
    monkeypatch.setattr(rds_idle, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("RDS_CPU_THRESHOLD", "5")
    monkeypatch.setenv("RDS_CONNECTIONS_THRESHOLD", "1")
    rds_idle.CPU_THRESHOLD = None
    rds_idle.CONNECTIONS_THRESHOLD = None

    results = rds_idle.scan_idle_rds()

    assert [r["id"] for r in results] == ["db-idle"]


def test_scan_idle_rds_skips_stopped(monkeypatch):
    """Test that stopped instances are not scanned for idle."""
    instances = [{
        "DBInstances": [
            _instance("db-stopped", status="stopped"),
            _instance("db-idle", status="available"),
        ]
    }]

    fake_rds = FakeRDSClient(instances_pages=instances)
    fake_cw = FakeCloudwatchClient({"db-idle": {"cpuutilization": [2.0], "databaseconnections": [0]}})

    monkeypatch.setattr(rds_idle, "_rds_client", fake_rds)
    monkeypatch.setattr(rds_idle, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("RDS_CPU_THRESHOLD", "5")
    monkeypatch.setenv("RDS_CONNECTIONS_THRESHOLD", "1")
    rds_idle.CPU_THRESHOLD = None
    rds_idle.CONNECTIONS_THRESHOLD = None

    results = rds_idle.scan_idle_rds()

    assert [r["id"] for r in results] == ["db-idle"]


def test_scan_idle_rds_custom_thresholds(monkeypatch):
    """Test custom CPU and connections thresholds from env vars."""
    instances = [{
        "DBInstances": [
            _instance("db-1"),
            _instance("db-2"),
        ]
    }]

    fake_rds = FakeRDSClient(instances_pages=instances)
    fake_cw = FakeCloudwatchClient({
        "db-1": {"cpuutilization": [3.0], "databaseconnections": [0]},
        "db-2": {"cpuutilization": [6.0], "databaseconnections": [2]},
    })

    def mock_list_tags(ResourceName):
        return {"TagList": []}
    fake_rds.list_tags_for_resource = mock_list_tags

    monkeypatch.setattr(rds_idle, "_rds_client", fake_rds)
    monkeypatch.setattr(rds_idle, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("RDS_CPU_THRESHOLD", "5")
    monkeypatch.setenv("RDS_CONNECTIONS_THRESHOLD", "1")
    rds_idle.CPU_THRESHOLD = None
    rds_idle.CONNECTIONS_THRESHOLD = None

    results = rds_idle.scan_idle_rds()

    assert [r["id"] for r in results] == ["db-1"]


def test_scan_idle_rds_no_metrics(monkeypatch):
    """Test handling of instances with no CloudWatch metrics."""
    instances = [{
        "DBInstances": [
            _instance("db-new"),
        ]
    }]

    fake_rds = FakeRDSClient(instances_pages=instances)
    fake_cw = FakeCloudwatchClient({"db-new": None})

    monkeypatch.setattr(rds_idle, "_rds_client", fake_rds)
    monkeypatch.setattr(rds_idle, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("RDS_CPU_THRESHOLD", "5")
    rds_idle.CPU_THRESHOLD = None
    rds_idle.CONNECTIONS_THRESHOLD = None

    results = rds_idle.scan_idle_rds()

    assert len(results) == 0


def test_scan_idle_rds_metric_batching(monkeypatch):
    """Test GetMetricData batching across 500-query limit."""
    n = 200  # 200 instances * 2 queries = 400 -> fits in one call (500 limit)
    instances = {"DBInstances": [_instance(f"db-{i}") for i in range(n)]}
    metrics_map = {f"db-{i}": {"cpuutilization": [2.0], "databaseconnections": [0]} for i in range(n)}

    fake_rds = FakeRDSClient(instances_pages=[instances])
    fake_cw = FakeCloudwatchClient(metrics_map)

    monkeypatch.setattr(rds_idle, "_rds_client", fake_rds)
    monkeypatch.setattr(rds_idle, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("RDS_CPU_THRESHOLD", "5")
    monkeypatch.setenv("RDS_CONNECTIONS_THRESHOLD", "1")
    rds_idle.CPU_THRESHOLD = None
    rds_idle.CONNECTIONS_THRESHOLD = None

    results = rds_idle.scan_idle_rds()

    assert len(results) == n
    assert len(fake_cw.calls) == 1
    assert len(fake_cw.calls[0]["MetricDataQueries"]) == 400


def test_scan_idle_rds_large_fleet_batching(monkeypatch):
    """Test batching with >250 instances (exceeds 500/2=250 per call)."""
    n = 300  # 300 instances * 2 queries = 600 -> must split into 2 calls
    instances = {"DBInstances": [_instance(f"db-{i}") for i in range(n)]}
    metrics_map = {f"db-{i}": {"cpuutilization": [2.0], "databaseconnections": [0]} for i in range(n)}

    fake_rds = FakeRDSClient(instances_pages=[instances])
    fake_cw = FakeCloudwatchClient(metrics_map)

    monkeypatch.setattr(rds_idle, "_rds_client", fake_rds)
    monkeypatch.setattr(rds_idle, "_cloudwatch_client", fake_cw)
    monkeypatch.setenv("RDS_CPU_THRESHOLD", "5")
    monkeypatch.setenv("RDS_CONNECTIONS_THRESHOLD", "1")
    rds_idle.CPU_THRESHOLD = None
    rds_idle.CONNECTIONS_THRESHOLD = None

    results = rds_idle.scan_idle_rds()

    assert len(results) == n
    assert len(fake_cw.calls) == 2
    assert len(fake_cw.calls[0]["MetricDataQueries"]) == 500
    assert len(fake_cw.calls[1]["MetricDataQueries"]) == 100