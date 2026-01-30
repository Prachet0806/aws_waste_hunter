import scanner.elb_scanner as elb_scanner


class FakeElbV2Paginator:
    def __init__(self, lbs_data):
        self.lbs_data = lbs_data

    def paginate(self):
        return self.lbs_data


class FakeElbV2Client:
    def __init__(self, lbs_data, tags_map=None):
        self.lbs_data = lbs_data
        self.tags_map = tags_map or {}
        self.tag_calls = []

    def get_paginator(self, name):
        assert name == "describe_load_balancers"
        return FakeElbV2Paginator(self.lbs_data)

    def describe_tags(self, ResourceArns=None):
        self.tag_calls.append(ResourceArns)
        tag_descriptions = []
        for arn in ResourceArns:
            tags = self.tags_map.get(arn, [])
            tag_descriptions.append({"ResourceArn": arn, "Tags": tags})
        return {"TagDescriptions": tag_descriptions}


class FakeClassicElbPaginator:
    def __init__(self, lbs_data):
        self.lbs_data = lbs_data

    def paginate(self):
        return self.lbs_data


class FakeClassicElbClient:
    def __init__(self, lbs_data):
        self.lbs_data = lbs_data

    def get_paginator(self, name):
        assert name == "describe_load_balancers"
        return FakeClassicElbPaginator(self.lbs_data)


class FakeCloudwatchClient:
    def __init__(self, metrics_map):
        self.metrics_map = metrics_map
        self.calls = []

    def get_metric_statistics(self, **kwargs):
        namespace = kwargs["Namespace"]
        metric_name = kwargs["MetricName"]
        dimension_value = kwargs["Dimensions"][0]["Value"]
        
        self.calls.append({
            "namespace": namespace,
            "metric": metric_name,
            "dimension": dimension_value
        })
        
        key = f"{namespace}:{metric_name}:{dimension_value}"
        datapoints = self.metrics_map.get(key, [])
        
        if not datapoints:
            return {"Datapoints": []}
        
        return {"Datapoints": [{"Sum": val} for val in datapoints]}


def test_scan_unused_elb_alb_with_tags(monkeypatch):
    """Test ALB scanning with proper metric dimension and tags."""
    lbs_data = [{
        "LoadBalancers": [{
            "LoadBalancerName": "my-alb",
            "LoadBalancerArn": "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/my-alb/abc123",
            "Type": "application",
            "Scheme": "internet-facing",
            "AvailabilityZones": [{"ZoneName": "us-east-1a"}],
        }]
    }]
    
    tags_map = {
        "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/my-alb/abc123": [
            {"Key": "owner", "Value": "sre"},
            {"Key": "env", "Value": "prod"}
        ]
    }
    
    fake_elbv2 = FakeElbV2Client(lbs_data, tags_map)
    fake_elb = FakeClassicElbClient([{"LoadBalancerDescriptions": []}])
    fake_cw = FakeCloudwatchClient({})  # No metrics = unused
    
    monkeypatch.setattr(elb_scanner, "_elbv2_client", fake_elbv2)
    monkeypatch.setattr(elb_scanner, "_elb_client", fake_elb)
    monkeypatch.setattr(elb_scanner, "_cloudwatch_client", fake_cw)

    results = elb_scanner.scan_unused_elb()

    assert len(results) == 1
    assert results[0]["id"] == "my-alb"
    assert results[0]["lb_type"] == "APPLICATION"
    assert results[0]["region"] == "us-east-1"
    assert results[0]["tags"]["owner"] == "sre"
    assert fake_cw.calls[0]["dimension"] == "app/my-alb/abc123"


def test_scan_unused_elb_nlb(monkeypatch):
    """Test Network Load Balancer scanning."""
    lbs_data = [{
        "LoadBalancers": [{
            "LoadBalancerName": "my-nlb",
            "LoadBalancerArn": "arn:aws:elasticloadbalancing:us-west-2:123:loadbalancer/net/my-nlb/xyz789",
            "Type": "network",
            "Scheme": "internal",
            "AvailabilityZones": [{"ZoneName": "us-west-2b"}],
        }]
    }]
    
    fake_elbv2 = FakeElbV2Client(lbs_data, {})
    fake_elb = FakeClassicElbClient([{"LoadBalancerDescriptions": []}])
    fake_cw = FakeCloudwatchClient({})
    
    monkeypatch.setattr(elb_scanner, "_elbv2_client", fake_elbv2)
    monkeypatch.setattr(elb_scanner, "_elb_client", fake_elb)
    monkeypatch.setattr(elb_scanner, "_cloudwatch_client", fake_cw)

    results = elb_scanner.scan_unused_elb()

    assert len(results) == 1
    assert results[0]["id"] == "my-nlb"
    assert results[0]["lb_type"] == "NETWORK"
    assert results[0]["region"] == "us-west-2"
    assert fake_cw.calls[0]["namespace"] == "AWS/NetworkELB"
    assert fake_cw.calls[0]["metric"] == "ProcessedBytes"


def test_scan_unused_elb_classic(monkeypatch):
    """Test Classic Load Balancer scanning."""
    classic_lbs_data = [{
        "LoadBalancerDescriptions": [{
            "LoadBalancerName": "my-classic-lb",
            "Scheme": "internet-facing",
            "AvailabilityZones": ["eu-west-1a", "eu-west-1b"],
        }]
    }]
    
    fake_elbv2 = FakeElbV2Client([{"LoadBalancers": []}])
    fake_elb = FakeClassicElbClient(classic_lbs_data)
    fake_cw = FakeCloudwatchClient({})
    
    monkeypatch.setattr(elb_scanner, "_elbv2_client", fake_elbv2)
    monkeypatch.setattr(elb_scanner, "_elb_client", fake_elb)
    monkeypatch.setattr(elb_scanner, "_cloudwatch_client", fake_cw)

    results = elb_scanner.scan_unused_elb()

    assert len(results) == 1
    assert results[0]["id"] == "my-classic-lb"
    assert results[0]["lb_type"] == "CLASSIC"
    assert results[0]["region"] == "eu-west-1"
    assert results[0]["tags"] == {}  # Classic doesn't support tags via API
    assert fake_cw.calls[0]["namespace"] == "AWS/ELB"


def test_scan_unused_elb_filters_used(monkeypatch):
    """Test that LBs with traffic are filtered out."""
    lbs_data = [{
        "LoadBalancers": [
            {
                "LoadBalancerName": "unused-alb",
                "LoadBalancerArn": "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/unused/abc",
                "Type": "application",
                "Scheme": "internet-facing",
                "AvailabilityZones": [{"ZoneName": "us-east-1a"}],
            },
            {
                "LoadBalancerName": "used-alb",
                "LoadBalancerArn": "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/used/def",
                "Type": "application",
                "Scheme": "internet-facing",
                "AvailabilityZones": [{"ZoneName": "us-east-1a"}],
            }
        ]
    }]
    
    metrics_map = {
        "AWS/ApplicationELB:RequestCount:app/unused/abc": [],  # No traffic
        "AWS/ApplicationELB:RequestCount:app/used/def": [1000, 2000]  # Has traffic
    }
    
    fake_elbv2 = FakeElbV2Client(lbs_data, {})
    fake_elb = FakeClassicElbClient([{"LoadBalancerDescriptions": []}])
    fake_cw = FakeCloudwatchClient(metrics_map)
    
    monkeypatch.setattr(elb_scanner, "_elbv2_client", fake_elbv2)
    monkeypatch.setattr(elb_scanner, "_elb_client", fake_elb)
    monkeypatch.setattr(elb_scanner, "_cloudwatch_client", fake_cw)

    results = elb_scanner.scan_unused_elb()

    assert len(results) == 1
    assert results[0]["id"] == "unused-alb"


def test_scan_unused_elb_batch_tags(monkeypatch):
    """Test batch tag fetching for multiple LBs."""
    lbs_data = [{
        "LoadBalancers": [
            {"LoadBalancerName": f"alb-{i}",
             "LoadBalancerArn": f"arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/alb-{i}/x{i}",
             "Type": "application",
             "Scheme": "internet-facing",
             "AvailabilityZones": [{"ZoneName": "us-east-1a"}]}
            for i in range(25)  # 25 LBs to test batching (should be 2 API calls)
        ]
    }]
    
    fake_elbv2 = FakeElbV2Client(lbs_data, {})
    fake_elb = FakeClassicElbClient([{"LoadBalancerDescriptions": []}])
    fake_cw = FakeCloudwatchClient({})
    
    monkeypatch.setattr(elb_scanner, "_elbv2_client", fake_elbv2)
    monkeypatch.setattr(elb_scanner, "_elb_client", fake_elb)
    monkeypatch.setattr(elb_scanner, "_cloudwatch_client", fake_cw)

    results = elb_scanner.scan_unused_elb()

    # Should have called describe_tags twice (20 + 5)
    assert len(fake_elbv2.tag_calls) == 2
    assert len(fake_elbv2.tag_calls[0]) == 20
    assert len(fake_elbv2.tag_calls[1]) == 5


def test_scan_unused_elb_empty_azs(monkeypatch):
    """Test handling of empty availability zones list."""
    lbs_data = [{
        "LoadBalancers": [{
            "LoadBalancerName": "no-az-lb",
            "LoadBalancerArn": "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/no-az/abc",
            "Type": "application",
            "Scheme": "internet-facing",
            "AvailabilityZones": [],  # Empty list
        }]
    }]
    
    fake_elbv2 = FakeElbV2Client(lbs_data, {})
    fake_elb = FakeClassicElbClient([{"LoadBalancerDescriptions": []}])
    fake_cw = FakeCloudwatchClient({})
    
    monkeypatch.setattr(elb_scanner, "_elbv2_client", fake_elbv2)
    monkeypatch.setattr(elb_scanner, "_elb_client", fake_elb)
    monkeypatch.setattr(elb_scanner, "_cloudwatch_client", fake_cw)

    results = elb_scanner.scan_unused_elb()

    # Should not crash
    assert len(results) == 1
    assert results[0]["az"] == ""
    assert results[0]["region"] is None
