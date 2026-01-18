import scanner.elb_scanner as elb_scanner


class FakeElbPaginator:
    def paginate(self):
        return [
            {
                "LoadBalancers": [
                    {
                        "LoadBalancerName": "lb-1",
                        "LoadBalancerArn": "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/test/abc",
                        "Scheme": "internet-facing",
                        "AvailabilityZones": [{"ZoneName": "us-east-1a"}],
                    }
                ]
            }
        ]


class FakeElb:
    def __init__(self):
        self.last_tag_arns = None

    def get_paginator(self, name):
        assert name == "describe_load_balancers"
        return FakeElbPaginator()

    def describe_tags(self, LoadBalancerArns=None):
        self.last_tag_arns = LoadBalancerArns
        return {"TagDescriptions": [{"Tags": [{"Key": "owner", "Value": "sre"}]}]}


class FakeCloudwatch:
    def __init__(self):
        self.dimensions = None

    def get_metric_statistics(self, **kwargs):
        self.dimensions = kwargs["Dimensions"]
        return {"Datapoints": []}


def test_scan_unused_elb_uses_metric_dimension_and_tags(monkeypatch):
    fake_elb = FakeElb()
    fake_cw = FakeCloudwatch()
    monkeypatch.setattr(elb_scanner, "elbv2", fake_elb)
    monkeypatch.setattr(elb_scanner, "cloudwatch", fake_cw)

    results = elb_scanner.scan_unused_elb()

    assert len(results) == 1
    assert results[0]["id"] == "lb-1"
    assert results[0]["tags"]["owner"] == "sre"
    assert fake_elb.last_tag_arns == [
        "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/test/abc"
    ]
    assert fake_cw.dimensions == [
        {"Name": "LoadBalancer", "Value": "app/test/abc"}
    ]
