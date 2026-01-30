import scanner.ebs_scanner as ebs_scanner


class FakeEC2Client:
    def __init__(self, volumes_data):
        self.volumes_data = volumes_data

    def get_paginator(self, name):
        assert name == "describe_volumes"
        return FakePaginator(self.volumes_data)


class FakePaginator:
    def __init__(self, volumes_data):
        self.volumes_data = volumes_data

    def paginate(self, Filters=None):
        return self.volumes_data


def test_scan_unattached_ebs_basic(monkeypatch):
    """Test basic EBS scanning with standard AZ."""
    volumes_data = [{
        "Volumes": [
            {
                "VolumeId": "vol-123",
                "Size": 100,
                "VolumeType": "gp3",
                "AvailabilityZone": "us-east-1a",
                "Tags": [{"Key": "owner", "Value": "sre"}]
            }
        ]
    }]
    
    fake_client = FakeEC2Client(volumes_data)
    monkeypatch.setattr(ebs_scanner, "_ec2_client", fake_client)

    results = ebs_scanner.scan_unattached_ebs()

    assert len(results) == 1
    assert results[0]["id"] == "vol-123"
    assert results[0]["size_gb"] == 100
    assert results[0]["volume_type"] == "gp3"
    assert results[0]["az"] == "us-east-1a"
    assert results[0]["region"] == "us-east-1"
    assert results[0]["tags"]["owner"] == "sre"


def test_scan_unattached_ebs_multiple_pages(monkeypatch):
    """Test pagination with multiple pages of volumes."""
    volumes_data = [
        {
            "Volumes": [
                {
                    "VolumeId": "vol-1",
                    "Size": 50,
                    "VolumeType": "gp2",
                    "AvailabilityZone": "us-west-2a",
                    "Tags": []
                }
            ]
        },
        {
            "Volumes": [
                {
                    "VolumeId": "vol-2",
                    "Size": 100,
                    "VolumeType": "io1",
                    "AvailabilityZone": "eu-west-1b",
                    "Tags": [{"Key": "env", "Value": "prod"}]
                }
            ]
        }
    ]
    
    fake_client = FakeEC2Client(volumes_data)
    monkeypatch.setattr(ebs_scanner, "_ec2_client", fake_client)

    results = ebs_scanner.scan_unattached_ebs()

    assert len(results) == 2
    assert results[0]["id"] == "vol-1"
    assert results[0]["region"] == "us-west-2"
    assert results[1]["id"] == "vol-2"
    assert results[1]["region"] == "eu-west-1"


def test_scan_unattached_ebs_local_zone(monkeypatch):
    """Test region extraction from Local Zone."""
    volumes_data = [{
        "Volumes": [
            {
                "VolumeId": "vol-local",
                "Size": 10,
                "VolumeType": "gp3",
                "AvailabilityZone": "us-east-1-bos-1a",
                "Tags": []
            }
        ]
    }]
    
    fake_client = FakeEC2Client(volumes_data)
    monkeypatch.setattr(ebs_scanner, "_ec2_client", fake_client)

    results = ebs_scanner.scan_unattached_ebs()

    assert len(results) == 1
    assert results[0]["az"] == "us-east-1-bos-1a"
    assert results[0]["region"] == "us-east-1"


def test_scan_unattached_ebs_no_tags(monkeypatch):
    """Test handling of volumes without tags."""
    volumes_data = [{
        "Volumes": [
            {
                "VolumeId": "vol-notags",
                "Size": 25,
                "VolumeType": "sc1",
                "AvailabilityZone": "ap-south-1a"
                # No Tags field
            }
        ]
    }]
    
    fake_client = FakeEC2Client(volumes_data)
    monkeypatch.setattr(ebs_scanner, "_ec2_client", fake_client)

    results = ebs_scanner.scan_unattached_ebs()

    assert len(results) == 1
    assert results[0]["tags"] == {}


def test_scan_unattached_ebs_empty_result(monkeypatch):
    """Test scanning when no unattached volumes exist."""
    volumes_data = [{"Volumes": []}]
    
    fake_client = FakeEC2Client(volumes_data)
    monkeypatch.setattr(ebs_scanner, "_ec2_client", fake_client)

    results = ebs_scanner.scan_unattached_ebs()

    assert len(results) == 0
