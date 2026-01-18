import lambda_handler


def test_handler_partial_on_scan_error(monkeypatch):
    def good_scan():
        return [{"type": "EBS", "id": "vol-1", "size_gb": 1, "az": "us-east-1a", "tags": {}}]

    def bad_scan():
        raise RuntimeError("boom")

    monkeypatch.setattr(lambda_handler, "scan_unattached_ebs", good_scan)
    monkeypatch.setattr(lambda_handler, "scan_idle_ec2", bad_scan)
    monkeypatch.setattr(lambda_handler, "scan_unused_elb", good_scan)
    monkeypatch.setattr(lambda_handler, "scan_stopped_rds", good_scan)

    monkeypatch.setattr(lambda_handler, "send_report", lambda report: None)
    monkeypatch.setattr(lambda_handler, "archive_report", lambda report: None)

    result = lambda_handler.handler({}, {})

    assert result["status"] == "partial"
    assert result["scan_errors"] == 1


def test_handler_partial_on_delivery_error(monkeypatch):
    def good_scan():
        return [{"type": "EBS", "id": "vol-1", "size_gb": 1, "az": "us-east-1a", "tags": {}}]

    def bad_delivery(report):
        raise RuntimeError("boom")

    monkeypatch.setattr(lambda_handler, "scan_unattached_ebs", good_scan)
    monkeypatch.setattr(lambda_handler, "scan_idle_ec2", good_scan)
    monkeypatch.setattr(lambda_handler, "scan_unused_elb", good_scan)
    monkeypatch.setattr(lambda_handler, "scan_stopped_rds", good_scan)

    monkeypatch.setattr(lambda_handler, "send_report", bad_delivery)
    monkeypatch.setattr(lambda_handler, "archive_report", lambda report: None)

    result = lambda_handler.handler({}, {})

    assert result["status"] == "partial"
    assert result["delivery_errors"] == 1
