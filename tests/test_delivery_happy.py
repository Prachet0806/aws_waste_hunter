"""Happy-path delivery tests (previously only failure paths)."""

from delivery import s3_archiver, sns_sender


class FakeSNS:
    def __init__(self):
        self.calls = []

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": "mid-123"}


class FakeS3:
    def __init__(self):
        self.calls = []

    def put_object(self, **kwargs):
        self.calls.append(kwargs)
        return {}


def test_send_report_happy(monkeypatch):
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:topic")
    fake = FakeSNS()
    monkeypatch.setattr(sns_sender, "_sns_client", fake)
    mid = sns_sender.send_report(
        "# report",
        {
            "monthly_waste": 10,
            "resources": 1,
            "high_waste": False,
            "status": "ok",
            "scan_errors": 0,
            "delivery_errors": 0,
            "no_metrics": 0,
        },
    )
    assert mid == "mid-123"
    assert fake.calls[0]["TopicArn"].endswith(":topic")
    assert "MessageAttributes" in fake.calls[0]


def test_send_report_truncates_oversize(monkeypatch):
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:topic")
    monkeypatch.setenv("REPORT_BUCKET", "my-bucket")
    fake = FakeSNS()
    monkeypatch.setattr(sns_sender, "_sns_client", fake)
    big = "x" * (sns_sender.SNS_MAX_BYTES + 1000)
    sns_sender.send_report(big, None)
    assert len(fake.calls[0]["Message"].encode("utf-8")) <= sns_sender.SNS_MAX_BYTES


def test_archive_report_happy(monkeypatch):
    monkeypatch.setenv("REPORT_BUCKET", "my-bucket")
    fake = FakeS3()
    monkeypatch.setattr(s3_archiver, "_s3_client", fake)
    key = s3_archiver.archive_report("# report")
    assert key.startswith("reports/")
    assert key.endswith(".md")
    assert fake.calls[0]["Bucket"] == "my-bucket"
    assert fake.calls[0]["ServerSideEncryption"] == "AES256"
