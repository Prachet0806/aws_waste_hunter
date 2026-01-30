import scanner.rds_scanner as rds_scanner


class FakeRDSClusterPaginator:
    def __init__(self, clusters_data):
        self.clusters_data = clusters_data

    def paginate(self):
        return self.clusters_data


class FakeRDSInstancePaginator:
    def __init__(self, instances_data):
        self.instances_data = instances_data

    def paginate(self):
        return self.instances_data


class FakeRDSClient:
    def __init__(self, clusters_data, instances_data, tags_map=None):
        self.clusters_data = clusters_data
        self.instances_data = instances_data
        self.tags_map = tags_map or {}
        self.tag_calls = []

    def get_paginator(self, name):
        if name == "describe_db_clusters":
            return FakeRDSClusterPaginator(self.clusters_data)
        elif name == "describe_db_instances":
            return FakeRDSInstancePaginator(self.instances_data)
        raise ValueError(f"Unknown paginator: {name}")

    def list_tags_for_resource(self, ResourceName=None):
        self.tag_calls.append(ResourceName)
        tags = self.tags_map.get(ResourceName, [])
        return {"TagList": tags}


def test_scan_stopped_rds_cluster_with_tags(monkeypatch):
    """Test scanning stopped RDS cluster with tags."""
    clusters_data = [{
        "DBClusters": [{
            "DBClusterIdentifier": "my-cluster",
            "DBClusterArn": "arn:aws:rds:us-east-1:123:cluster:my-cluster",
            "Status": "stopped",
            "Engine": "aurora-mysql",
            "AvailabilityZones": ["us-east-1a", "us-east-1b"],
        }]
    }]
    
    instances_data = [{"DBInstances": []}]
    
    tags_map = {
        "arn:aws:rds:us-east-1:123:cluster:my-cluster": [
            {"Key": "owner", "Value": "sre"},
            {"Key": "env", "Value": "staging"}
        ]
    }
    
    fake_rds = FakeRDSClient(clusters_data, instances_data, tags_map)
    monkeypatch.setattr(rds_scanner, "_rds_client", fake_rds)

    results = rds_scanner.scan_stopped_rds()

    assert len(results) == 1
    assert results[0]["type"] == "RDS_CLUSTER"
    assert results[0]["id"] == "my-cluster"
    assert results[0]["engine"] == "aurora-mysql"
    assert results[0]["region"] == "us-east-1"
    assert results[0]["tags"]["owner"] == "sre"
    assert results[0]["tags"]["env"] == "staging"


def test_scan_stopped_rds_instance(monkeypatch):
    """Test scanning stopped RDS standalone instance."""
    clusters_data = [{"DBClusters": []}]
    
    instances_data = [{
        "DBInstances": [{
            "DBInstanceIdentifier": "my-db-instance",
            "DBInstanceArn": "arn:aws:rds:eu-west-1:456:db:my-db-instance",
            "DBInstanceStatus": "stopped",
            "Engine": "postgres",
            "DBInstanceClass": "db.t3.medium",
            "AvailabilityZone": "eu-west-1c",
        }]
    }]
    
    tags_map = {
        "arn:aws:rds:eu-west-1:456:db:my-db-instance": [
            {"Key": "team", "Value": "backend"}
        ]
    }
    
    fake_rds = FakeRDSClient(clusters_data, instances_data, tags_map)
    monkeypatch.setattr(rds_scanner, "_rds_client", fake_rds)

    results = rds_scanner.scan_stopped_rds()

    assert len(results) == 1
    assert results[0]["type"] == "RDS_INSTANCE"
    assert results[0]["id"] == "my-db-instance"
    assert results[0]["engine"] == "postgres"
    assert results[0]["instance_class"] == "db.t3.medium"
    assert results[0]["region"] == "eu-west-1"
    assert results[0]["tags"]["team"] == "backend"


def test_scan_stopped_rds_both_cluster_and_instance(monkeypatch):
    """Test scanning both stopped clusters and instances."""
    clusters_data = [{
        "DBClusters": [{
            "DBClusterIdentifier": "aurora-cluster",
            "DBClusterArn": "arn:aws:rds:us-west-2:123:cluster:aurora-cluster",
            "Status": "stopped",
            "Engine": "aurora",
            "AvailabilityZones": ["us-west-2a"],
        }]
    }]
    
    instances_data = [{
        "DBInstances": [{
            "DBInstanceIdentifier": "mysql-instance",
            "DBInstanceArn": "arn:aws:rds:us-west-2:123:db:mysql-instance",
            "DBInstanceStatus": "stopped",
            "Engine": "mysql",
            "DBInstanceClass": "db.t3.small",
            "AvailabilityZone": "us-west-2b",
        }]
    }]
    
    fake_rds = FakeRDSClient(clusters_data, instances_data, {})
    monkeypatch.setattr(rds_scanner, "_rds_client", fake_rds)

    results = rds_scanner.scan_stopped_rds()

    assert len(results) == 2
    cluster_results = [r for r in results if r["type"] == "RDS_CLUSTER"]
    instance_results = [r for r in results if r["type"] == "RDS_INSTANCE"]
    
    assert len(cluster_results) == 1
    assert len(instance_results) == 1


def test_scan_stopped_rds_filters_running(monkeypatch):
    """Test that running clusters/instances are filtered out."""
    clusters_data = [{
        "DBClusters": [
            {
                "DBClusterIdentifier": "stopped-cluster",
                "DBClusterArn": "arn:aws:rds:us-east-1:123:cluster:stopped-cluster",
                "Status": "stopped",
                "Engine": "aurora",
                "AvailabilityZones": ["us-east-1a"],
            },
            {
                "DBClusterIdentifier": "running-cluster",
                "DBClusterArn": "arn:aws:rds:us-east-1:123:cluster:running-cluster",
                "Status": "available",
                "Engine": "aurora",
                "AvailabilityZones": ["us-east-1a"],
            }
        ]
    }]
    
    instances_data = [{
        "DBInstances": [
            {
                "DBInstanceIdentifier": "stopped-instance",
                "DBInstanceArn": "arn:aws:rds:us-east-1:123:db:stopped-instance",
                "DBInstanceStatus": "stopped",
                "Engine": "postgres",
                "DBInstanceClass": "db.t3.micro",
                "AvailabilityZone": "us-east-1a",
            },
            {
                "DBInstanceIdentifier": "running-instance",
                "DBInstanceArn": "arn:aws:rds:us-east-1:123:db:running-instance",
                "DBInstanceStatus": "available",
                "Engine": "mysql",
                "DBInstanceClass": "db.t3.micro",
                "AvailabilityZone": "us-east-1a",
            }
        ]
    }]
    
    fake_rds = FakeRDSClient(clusters_data, instances_data, {})
    monkeypatch.setattr(rds_scanner, "_rds_client", fake_rds)

    results = rds_scanner.scan_stopped_rds()

    assert len(results) == 2
    ids = [r["id"] for r in results]
    assert "stopped-cluster" in ids
    assert "stopped-instance" in ids
    assert "running-cluster" not in ids
    assert "running-instance" not in ids


def test_scan_stopped_rds_empty_azs(monkeypatch):
    """Test handling of empty availability zones."""
    clusters_data = [{
        "DBClusters": [{
            "DBClusterIdentifier": "no-az-cluster",
            "DBClusterArn": "arn:aws:rds:us-east-1:123:cluster:no-az-cluster",
            "Status": "stopped",
            "Engine": "aurora",
            "AvailabilityZones": [],  # Empty list
        }]
    }]
    
    instances_data = [{"DBInstances": []}]
    
    fake_rds = FakeRDSClient(clusters_data, instances_data, {})
    monkeypatch.setattr(rds_scanner, "_rds_client", fake_rds)

    results = rds_scanner.scan_stopped_rds()

    # Should not crash
    assert len(results) == 1
    assert results[0]["az"] == ""
    assert results[0]["region"] is None


def test_scan_stopped_rds_pagination(monkeypatch):
    """Test pagination for both clusters and instances."""
    clusters_data = [
        {"DBClusters": [{
            "DBClusterIdentifier": "cluster-1",
            "DBClusterArn": "arn:aws:rds:us-east-1:123:cluster:cluster-1",
            "Status": "stopped",
            "Engine": "aurora",
            "AvailabilityZones": ["us-east-1a"],
        }]},
        {"DBClusters": [{
            "DBClusterIdentifier": "cluster-2",
            "DBClusterArn": "arn:aws:rds:us-east-1:123:cluster:cluster-2",
            "Status": "stopped",
            "Engine": "aurora-postgresql",
            "AvailabilityZones": ["us-east-1b"],
        }]}
    ]
    
    instances_data = [
        {"DBInstances": [{
            "DBInstanceIdentifier": "instance-1",
            "DBInstanceArn": "arn:aws:rds:us-east-1:123:db:instance-1",
            "DBInstanceStatus": "stopped",
            "Engine": "mysql",
            "DBInstanceClass": "db.t3.micro",
            "AvailabilityZone": "us-east-1a",
        }]},
        {"DBInstances": [{
            "DBInstanceIdentifier": "instance-2",
            "DBInstanceArn": "arn:aws:rds:us-east-1:123:db:instance-2",
            "DBInstanceStatus": "stopped",
            "Engine": "postgres",
            "DBInstanceClass": "db.t3.small",
            "AvailabilityZone": "us-east-1c",
        }]}
    ]
    
    fake_rds = FakeRDSClient(clusters_data, instances_data, {})
    monkeypatch.setattr(rds_scanner, "_rds_client", fake_rds)

    results = rds_scanner.scan_stopped_rds()

    assert len(results) == 4
    cluster_ids = [r["id"] for r in results if r["type"] == "RDS_CLUSTER"]
    instance_ids = [r["id"] for r in results if r["type"] == "RDS_INSTANCE"]
    
    assert set(cluster_ids) == {"cluster-1", "cluster-2"}
    assert set(instance_ids) == {"instance-1", "instance-2"}
