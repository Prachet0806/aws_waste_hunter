import pytest
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
import scanner.snapshot_scanner as snapshot_scanner


class FakeEC2Paginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kwargs):
        return self.pages


class FakeEC2Client:
    def __init__(self, images_pages=None, snapshots_pages=None):
        self._region = "us-east-1"
        self.meta = SimpleNamespace(region_name=self._region)
        self.images_pages = images_pages or [{"Images": []}]
        self.snapshots_pages = snapshots_pages or [{"Snapshots": []}]

    def describe_snapshot_attribute(self, **kwargs):
        return {"CreateVolumePermissions": []}

    def get_paginator(self, name):
        if name == "describe_images":
            return FakeEC2Paginator(self.images_pages)
        if name == "describe_snapshots":
            return FakeEC2Paginator(self.snapshots_pages)
        raise AssertionError(f"unexpected paginator {name}")


def _snapshot(snap_id, age_days, volume_size=10, volume_id="vol-1", tags=None):
    return {
        "SnapshotId": snap_id,
        "VolumeSize": volume_size,
        "VolumeId": volume_id,
        "StartTime": datetime.now(timezone.utc) - timedelta(days=age_days),
        "Tags": tags or [],
    }


def _ami(snapshot_ids):
    return {
        "Images": [{
            "BlockDeviceMappings": [
                {"Ebs": {"SnapshotId": sid}} for sid in snapshot_ids
            ]
        }]
    }


def test_scan_orphaned_snapshots_basic(monkeypatch):
    """Test basic orphaned snapshot detection."""
    pages = [{"Snapshots": [
        _snapshot("snap-1", age_days=30, volume_size=100, tags=[{"Key": "owner", "Value": "sre"}]),
    ]}]

    fake = FakeEC2Client(snapshots_pages=pages)
    monkeypatch.setattr(snapshot_scanner, "_ec2_client", fake)
    monkeypatch.setenv("MIN_SNAPSHOT_AGE_DAYS", "7")

    results = snapshot_scanner.scan_orphaned_snapshots()

    assert len(results) == 1
    assert results[0]["type"] == "SNAPSHOT"
    assert results[0]["id"] == "snap-1"
    assert results[0]["size_gb"] == 100
    assert results[0]["volume_id"] == "vol-1"
    assert results[0]["region"] == "us-east-1"
    assert results[0]["tags"]["owner"] == "sre"
    assert results[0]["days_old"] >= 29.0


def test_scan_orphaned_snapshots_skips_ami_referenced(monkeypatch):
    """Test snapshots referenced by AMIs are not orphaned."""
    images = [_ami(["snap-inuse"])]
    pages = [{"Snapshots": [
        _snapshot("snap-inuse", age_days=30),
        _snapshot("snap-orphan", age_days=30),
    ]}]

    fake = FakeEC2Client(images_pages=images, snapshots_pages=pages)
    monkeypatch.setattr(snapshot_scanner, "_ec2_client", fake)
    monkeypatch.setenv("MIN_SNAPSHOT_AGE_DAYS", "7")

    results = snapshot_scanner.scan_orphaned_snapshots()

    assert [r["id"] for r in results] == ["snap-orphan"]


def test_scan_orphaned_snapshots_skips_recent(monkeypatch):
    """Test snapshots younger than min age are skipped."""
    pages = [{"Snapshots": [
        _snapshot("snap-new", age_days=1),
        _snapshot("snap-old", age_days=30),
    ]}]

    fake = FakeEC2Client(snapshots_pages=pages)
    monkeypatch.setattr(snapshot_scanner, "_ec2_client", fake)
    monkeypatch.setenv("MIN_SNAPSHOT_AGE_DAYS", "7")

    results = snapshot_scanner.scan_orphaned_snapshots()

    assert [r["id"] for r in results] == ["snap-old"]


def test_scan_orphaned_snapshots_custom_min_age(monkeypatch):
    """Test custom minimum age from env var."""
    pages = [{"Snapshots": [
        _snapshot("snap-5d", age_days=5),
        _snapshot("snap-15d", age_days=15),
    ]}]

    fake = FakeEC2Client(snapshots_pages=pages)
    monkeypatch.setattr(snapshot_scanner, "_ec2_client", fake)
    monkeypatch.setenv("MIN_SNAPSHOT_AGE_DAYS", "10")

    results = snapshot_scanner.scan_orphaned_snapshots()

    assert [r["id"] for r in results] == ["snap-15d"]


def test_scan_orphaned_snapshots_invalid_min_age(monkeypatch):
    """Test invalid MIN_SNAPSHOT_AGE_DAYS falls back to default."""
    pages = [{"Snapshots": [
        _snapshot("snap-5d", age_days=5),
        _snapshot("snap-30d", age_days=30),
    ]}]

    fake = FakeEC2Client(snapshots_pages=pages)
    monkeypatch.setattr(snapshot_scanner, "_ec2_client", fake)
    monkeypatch.setenv("MIN_SNAPSHOT_AGE_DAYS", "not-a-number")

    with pytest.raises(ValueError):
        snapshot_scanner.scan_orphaned_snapshots()


def test_scan_orphaned_snapshots_no_tags(monkeypatch):
    """Test snapshot without Tags field."""
    snap = _snapshot("snap-1", age_days=30)
    del snap["Tags"]
    pages = [{"Snapshots": [snap]}]

    fake = FakeEC2Client(snapshots_pages=pages)
    monkeypatch.setattr(snapshot_scanner, "_ec2_client", fake)
    monkeypatch.setenv("MIN_SNAPSHOT_AGE_DAYS", "7")

    results = snapshot_scanner.scan_orphaned_snapshots()

    assert results[0]["tags"] == {}


def test_scan_orphaned_snapshots_empty(monkeypatch):
    """Test no snapshots returns empty list."""
    fake = FakeEC2Client()
    monkeypatch.setattr(snapshot_scanner, "_ec2_client", fake)
    monkeypatch.setenv("MIN_SNAPSHOT_AGE_DAYS", "7")

    results = snapshot_scanner.scan_orphaned_snapshots()

    assert results == []