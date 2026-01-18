import scanner.ec2_scanner as ec2_scanner


class FakeEC2Paginator:
    def paginate(self, Filters=None):
        return [
            {
                "Reservations": [
                    {"Instances": [{"InstanceId": "i-1", "InstanceType": "t3.micro", "Placement": {"AvailabilityZone": "us-east-1a"}, "Tags": [{"Key": "owner", "Value": "sre"}]}]}
                ]
            },
            {
                "Reservations": [
                    {"Instances": [{"InstanceId": "i-2", "InstanceType": "t3.small", "Placement": {"AvailabilityZone": "us-east-1b"}, "Tags": []}]}
                ]
            },
        ]


class FakeEC2:
    def get_paginator(self, name):
        assert name == "describe_instances"
        return FakeEC2Paginator()


class FakeCloudwatch:
    def __init__(self, averages):
        self.averages = averages

    def get_metric_statistics(self, **kwargs):
        iid = kwargs["Dimensions"][0]["Value"]
        avg = self.averages[iid]
        return {"Datapoints": [{"Average": avg}]}


def test_scan_idle_ec2_paginates_and_filters(monkeypatch):
    monkeypatch.setattr(ec2_scanner, "ec2", FakeEC2())
    monkeypatch.setattr(ec2_scanner, "cloudwatch", FakeCloudwatch({"i-1": 1.0, "i-2": 10.0}))
    monkeypatch.setattr(ec2_scanner, "CPU_THRESHOLD", 2)

    results = ec2_scanner.scan_idle_ec2()

    assert len(results) == 1
    assert results[0]["id"] == "i-1"
    assert results[0]["tags"]["owner"] == "sre"
