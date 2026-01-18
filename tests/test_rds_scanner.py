import scanner.rds_scanner as rds_scanner


class FakeRDSPaginator:
    def paginate(self):
        return [
            {
                "DBClusters": [
                    {
                        "DBClusterIdentifier": "db-1",
                        "DBClusterArn": "arn:aws:rds:us-east-1:123:cluster:db-1",
                        "Status": "stopped",
                        "Engine": "aurora",
                        "AvailabilityZones": ["us-east-1a"],
                    }
                ]
            }
        ]


class FakeRDS:
    def __init__(self):
        self.tag_arns = []

    def get_paginator(self, name):
        assert name == "describe_db_clusters"
        return FakeRDSPaginator()

    def list_tags_for_resource(self, ResourceName=None):
        self.tag_arns.append(ResourceName)
        return {"TagList": [{"Key": "owner", "Value": "sre"}]}


def test_scan_stopped_rds_includes_tags(monkeypatch):
    fake_rds = FakeRDS()
    monkeypatch.setattr(rds_scanner, "rds", fake_rds)

    results = rds_scanner.scan_stopped_rds()

    assert len(results) == 1
    assert results[0]["id"] == "db-1"
    assert results[0]["tags"]["owner"] == "sre"
    assert fake_rds.tag_arns == ["arn:aws:rds:us-east-1:123:cluster:db-1"]
