import pytest

from delivery.s3_archiver import archive_report
from delivery.sns_sender import send_report


def test_send_report_requires_env(monkeypatch):
    monkeypatch.delenv("SNS_TOPIC_ARN", raising=False)
    with pytest.raises(ValueError):
        send_report("hi")


def test_archive_report_requires_env(monkeypatch):
    monkeypatch.delenv("REPORT_BUCKET", raising=False)
    with pytest.raises(ValueError):
        archive_report("hi")
