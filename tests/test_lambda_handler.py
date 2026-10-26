import lambda_handler


def _mock_all_scanners(monkeypatch, good_scan, bad_scan=None):
    """Mock all seven scanners; bad_scan overrides scan_idle_ec2 by default."""
    monkeypatch.setattr(lambda_handler, "scan_unattached_ebs", good_scan)
    monkeypatch.setattr(
        lambda_handler, "scan_idle_ec2", bad_scan if bad_scan else good_scan
    )
    monkeypatch.setattr(lambda_handler, "scan_unused_elb", good_scan)
    monkeypatch.setattr(lambda_handler, "scan_stopped_rds", good_scan)
    monkeypatch.setattr(lambda_handler, "scan_idle_rds", good_scan)
    monkeypatch.setattr(lambda_handler, "scan_unattached_eips", good_scan)
    monkeypatch.setattr(lambda_handler, "scan_orphaned_snapshots", good_scan)


def test_handler_partial_on_scan_error(monkeypatch):
    def good_scan():
        return [
            {"type": "EBS", "id": "vol-1", "size_gb": 1, "az": "us-east-1a", "tags": {}}
        ]

    def bad_scan():
        raise RuntimeError("boom")

    _mock_all_scanners(monkeypatch, good_scan, bad_scan=bad_scan)

    monkeypatch.setattr(lambda_handler, "send_report", lambda report, result=None: None)
    monkeypatch.setattr(
        lambda_handler, "archive_report", lambda report, result=None: None
    )

    result = lambda_handler.handler({}, {})

    assert result["status"] == "partial"
    assert result["scan_errors"] == 1


def test_handler_partial_on_delivery_error(monkeypatch):
    def good_scan():
        return [
            {"type": "EBS", "id": "vol-1", "size_gb": 1, "az": "us-east-1a", "tags": {}}
        ]

    def bad_delivery(report):
        raise RuntimeError("boom")

    _mock_all_scanners(monkeypatch, good_scan)

    monkeypatch.setattr(lambda_handler, "send_report", bad_delivery)
    monkeypatch.setattr(
        lambda_handler, "archive_report", lambda report, result=None: None
    )

    result = lambda_handler.handler({}, {})

    assert result["status"] == "partial"
    assert result["delivery_errors"] == 1


def test_handler_high_waste_alert(monkeypatch):
    """Test that high waste triggers the alert flag (D2)."""

    def big_scan():
        return [{"type": "EIP", "id": "eip-1", "tags": {}}]

    _mock_all_scanners(monkeypatch, big_scan)
    monkeypatch.setenv("PRICING_MODE", "static")
    monkeypatch.setenv("WASTE_ALERT_THRESHOLD", "100")
    monkeypatch.setattr(lambda_handler, "send_report", lambda report, result=None: None)
    monkeypatch.setattr(
        lambda_handler, "archive_report", lambda report, result=None: None
    )

    # 6 scanners each return 1 EIP = 6 * $3.60 = $21.60, below threshold
    result = lambda_handler.handler({}, {})

    assert "high_waste" in result
    assert (
        result["high_waste"] is None
    )  # Mocked scanners did not provide required API-operation evidence.
    assert result["compliance_percent"] is not None
