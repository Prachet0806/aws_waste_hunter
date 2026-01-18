import scanner.ebs_scanner as ebs_scanner

class FakeEC2:
    def get_paginator(self, name):
        assert name == "describe_volumes"
        return self

    def paginate(self, Filters=None):
        return [{
            "Volumes": [
                {
                    "VolumeId": "vol-123",
                    "Size": 100,
                    "AvailabilityZone": "us-east-1a",
                    "Tags": [{"Key": "owner", "Value": "sre"}]
                }
            ]
        }]

def test_scan_unattached_ebs(monkeypatch):
    monkeypatch.setattr(ebs_scanner, "ec2", FakeEC2())

    results = ebs_scanner.scan_unattached_ebs()

    assert len(results) == 1
    assert results[0]["id"] == "vol-123"
    assert results[0]["size_gb"] == 100
    assert results[0]["tags"]["owner"] == "sre"
