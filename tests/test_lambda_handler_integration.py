import pytest
import lambda_handler


class TestHandlerIntegration:
    """Integration tests for the full Lambda handler."""

    def test_full_scan_success(self, monkeypatch):
        """Test complete successful scan with all scanners."""
        def mock_scan_ebs():
            return [{"type": "EBS", "id": "vol-1", "size_gb": 10, "tags": {}}]

        def mock_scan_ec2():
            return [{"type": "EC2", "id": "i-1", "instance_type": "t3.micro", "tags": {}}]

        def mock_scan_elb():
            return [{"type": "ELB", "id": "lb-1", "tags": {}}]

        def mock_scan_rds():
            return [{"type": "RDS", "id": "db-1", "tags": {}}]

        def mock_send(report):
            pass

        def mock_archive(report):
            pass

        monkeypatch.setattr(lambda_handler, "scan_unattached_ebs", mock_scan_ebs)
        monkeypatch.setattr(lambda_handler, "scan_idle_ec2", mock_scan_ec2)
        monkeypatch.setattr(lambda_handler, "scan_unused_elb", mock_scan_elb)
        monkeypatch.setattr(lambda_handler, "scan_stopped_rds", mock_scan_rds)
        monkeypatch.setattr(lambda_handler, "send_report", mock_send)
        monkeypatch.setattr(lambda_handler, "archive_report", mock_archive)
        monkeypatch.setenv("PRICING_MODE", "static")

        result = lambda_handler.handler({}, {})

        assert result["status"] == "ok"
        assert result["resources"] == 4
        assert result["monthly_waste"] > 0
        assert result["scan_errors"] == 0
        assert result["delivery_errors"] == 0

    def test_scan_partial_failure(self, monkeypatch):
        """Test handler with some scanner failures."""
        def mock_scan_ebs():
            raise RuntimeError("EBS API unavailable")

        def mock_scan_ec2():
            return [{"type": "EC2", "id": "i-1", "instance_type": "t3.micro", "tags": {}}]

        def mock_scan_elb():
            raise RuntimeError("ELB throttled")

        def mock_scan_rds():
            return []

        def mock_send(report):
            pass

        def mock_archive(report):
            pass

        monkeypatch.setattr(lambda_handler, "scan_unattached_ebs", mock_scan_ebs)
        monkeypatch.setattr(lambda_handler, "scan_idle_ec2", mock_scan_ec2)
        monkeypatch.setattr(lambda_handler, "scan_unused_elb", mock_scan_elb)
        monkeypatch.setattr(lambda_handler, "scan_stopped_rds", mock_scan_rds)
        monkeypatch.setattr(lambda_handler, "send_report", mock_send)
        monkeypatch.setattr(lambda_handler, "archive_report", mock_archive)
        monkeypatch.setenv("PRICING_MODE", "static")

        result = lambda_handler.handler({}, {})

        assert result["status"] == "partial"
        assert result["resources"] == 1  # Only EC2 succeeded
        assert result["scan_errors"] == 2  # EBS and ELB failed
        assert result["delivery_errors"] == 0

    def test_delivery_failure(self, monkeypatch):
        """Test handler with delivery failures."""
        def mock_scan():
            return []

        def mock_send(report):
            raise RuntimeError("SNS unavailable")

        def mock_archive(report):
            pass

        monkeypatch.setattr(lambda_handler, "scan_unattached_ebs", mock_scan)
        monkeypatch.setattr(lambda_handler, "scan_idle_ec2", mock_scan)
        monkeypatch.setattr(lambda_handler, "scan_unused_elb", mock_scan)
        monkeypatch.setattr(lambda_handler, "scan_stopped_rds", mock_scan)
        monkeypatch.setattr(lambda_handler, "send_report", mock_send)
        monkeypatch.setattr(lambda_handler, "archive_report", mock_archive)
        monkeypatch.setenv("PRICING_MODE", "static")

        result = lambda_handler.handler({}, {})

        assert result["status"] == "partial"
        assert result["scan_errors"] == 0
        assert result["delivery_errors"] == 1

    def test_all_scanners_fail(self, monkeypatch):
        """Test handler when all scanners fail."""
        def mock_fail():
            raise RuntimeError("AWS API error")

        def mock_send(report):
            pass

        def mock_archive(report):
            pass

        monkeypatch.setattr(lambda_handler, "scan_unattached_ebs", mock_fail)
        monkeypatch.setattr(lambda_handler, "scan_idle_ec2", mock_fail)
        monkeypatch.setattr(lambda_handler, "scan_unused_elb", mock_fail)
        monkeypatch.setattr(lambda_handler, "scan_stopped_rds", mock_fail)
        monkeypatch.setattr(lambda_handler, "send_report", mock_send)
        monkeypatch.setattr(lambda_handler, "archive_report", mock_archive)
        monkeypatch.setenv("PRICING_MODE", "static")

        result = lambda_handler.handler({}, {})

        assert result["status"] == "partial"
        assert result["resources"] == 0
        assert result["scan_errors"] == 4
        assert result["monthly_waste"] == 0

    def test_all_delivery_fails(self, monkeypatch):
        """Test handler when all delivery methods fail."""
        def mock_scan():
            return [{"type": "EBS", "id": "vol-1", "size_gb": 10, "tags": {}}]

        def mock_fail(report):
            raise RuntimeError("Delivery failed")

        monkeypatch.setattr(lambda_handler, "scan_unattached_ebs", mock_scan)
        monkeypatch.setattr(lambda_handler, "scan_idle_ec2", lambda: [])
        monkeypatch.setattr(lambda_handler, "scan_unused_elb", lambda: [])
        monkeypatch.setattr(lambda_handler, "scan_stopped_rds", lambda: [])
        monkeypatch.setattr(lambda_handler, "send_report", mock_fail)
        monkeypatch.setattr(lambda_handler, "archive_report", mock_fail)
        monkeypatch.setenv("PRICING_MODE", "static")

        result = lambda_handler.handler({}, {})

        assert result["status"] == "partial"
        assert result["scan_errors"] == 0
        assert result["delivery_errors"] == 2

    def test_estimator_failure(self, monkeypatch):
        """Test handler when cost estimation fails."""
        def mock_scan():
            return [{"type": "EBS", "id": "vol-1", "size_gb": 10, "tags": {}}]

        def mock_estimate(resources):
            raise RuntimeError("Pricing API error")

        def mock_send(report):
            pass

        def mock_archive(report):
            pass

        monkeypatch.setattr(lambda_handler, "scan_unattached_ebs", mock_scan)
        monkeypatch.setattr(lambda_handler, "scan_idle_ec2", lambda: [])
        monkeypatch.setattr(lambda_handler, "scan_unused_elb", lambda: [])
        monkeypatch.setattr(lambda_handler, "scan_stopped_rds", lambda: [])
        monkeypatch.setattr(lambda_handler, "estimate_monthly_waste", mock_estimate)
        monkeypatch.setattr(lambda_handler, "send_report", mock_send)
        monkeypatch.setattr(lambda_handler, "archive_report", mock_archive)

        result = lambda_handler.handler({}, {})

        assert result["status"] == "partial"
        assert result["scan_errors"] == 1  # Estimation error tracked
        assert result["monthly_waste"] == 0

    def test_compliance_check_failure(self, monkeypatch):
        """Test handler when compliance checking fails."""
        def mock_scan():
            return [{"type": "EBS", "id": "vol-1", "size_gb": 10, "tags": {}}]

        def mock_check(resources):
            raise RuntimeError("Tag check error")

        def mock_send(report):
            pass

        def mock_archive(report):
            pass

        monkeypatch.setattr(lambda_handler, "scan_unattached_ebs", mock_scan)
        monkeypatch.setattr(lambda_handler, "scan_idle_ec2", lambda: [])
        monkeypatch.setattr(lambda_handler, "scan_unused_elb", lambda: [])
        monkeypatch.setattr(lambda_handler, "scan_stopped_rds", lambda: [])
        monkeypatch.setattr(lambda_handler, "check_tag_compliance", mock_check)
        monkeypatch.setattr(lambda_handler, "send_report", mock_send)
        monkeypatch.setattr(lambda_handler, "archive_report", mock_archive)
        monkeypatch.setenv("PRICING_MODE", "static")

        result = lambda_handler.handler({}, {})

        assert result["status"] == "partial"
        assert result["scan_errors"] == 1  # Compliance error tracked

    def test_empty_scan_results(self, monkeypatch):
        """Test handler when no waste is found."""
        def mock_scan():
            return []

        def mock_send(report):
            pass

        def mock_archive(report):
            pass

        monkeypatch.setattr(lambda_handler, "scan_unattached_ebs", mock_scan)
        monkeypatch.setattr(lambda_handler, "scan_idle_ec2", mock_scan)
        monkeypatch.setattr(lambda_handler, "scan_unused_elb", mock_scan)
        monkeypatch.setattr(lambda_handler, "scan_stopped_rds", mock_scan)
        monkeypatch.setattr(lambda_handler, "send_report", mock_send)
        monkeypatch.setattr(lambda_handler, "archive_report", mock_archive)
        monkeypatch.setenv("PRICING_MODE", "static")

        result = lambda_handler.handler({}, {})

        assert result["status"] == "ok"
        assert result["resources"] == 0
        assert result["monthly_waste"] == 0
        assert result["scan_errors"] == 0

    def test_error_details_captured(self, monkeypatch):
        """Test that error details are captured."""
        error_message = "Specific error message"

        def mock_fail():
            raise ValueError(error_message)

        def mock_scan():
            return []

        def mock_send(report):
            # Check that error is in report
            assert error_message in report
            assert "ValueError" in report

        def mock_archive(report):
            pass

        monkeypatch.setattr(lambda_handler, "scan_unattached_ebs", mock_fail)
        monkeypatch.setattr(lambda_handler, "scan_idle_ec2", mock_scan)
        monkeypatch.setattr(lambda_handler, "scan_unused_elb", mock_scan)
        monkeypatch.setattr(lambda_handler, "scan_stopped_rds", mock_scan)
        monkeypatch.setattr(lambda_handler, "send_report", mock_send)
        monkeypatch.setattr(lambda_handler, "archive_report", mock_archive)
        monkeypatch.setenv("PRICING_MODE", "static")

        result = lambda_handler.handler({}, {})

        assert result["status"] == "partial"
        assert result["scan_errors"] == 1
