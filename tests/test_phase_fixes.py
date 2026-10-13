"""Regression tests for P0-P4 fixes (fail-open, live/static cache, schema)."""
import json

import pytest

from utils.aws_helpers import chunk_list, normalize_tags, get_region_from_az
import scanner.elb_scanner as elb_scanner
import cost_engine.estimator as estimator
from reporting.report_builder import build_report
import compliance.tag_checker as tag_checker
import lambda_handler


def test_chunk_list_validates_size():
    with pytest.raises(ValueError):
        list(chunk_list([1, 2], 0))
    with pytest.raises(ValueError):
        list(chunk_list([1, 2], -1))
    with pytest.raises(ValueError):
        list(chunk_list(None, 2))


def test_normalize_tags_shapes():
    assert normalize_tags(None) == {}
    assert normalize_tags({"a": "b"}) == {"a": "b"}
    assert normalize_tags([{"Key": "k", "Value": "v"}]) == {"k": "v"}
    assert normalize_tags([{"Key": "k"}]) == {"k": ""}
    assert normalize_tags("nope") == {}


def test_region_malformed_returns_none():
    assert get_region_from_az("us") is None
    assert get_region_from_az("us-east") is None
    assert get_region_from_az(None) is None


def test_elb_fail_open_on_missing_datapoints(monkeypatch):
    """Missing CloudWatch datapoints must skip (fail-open), not flag unused."""
    lbs_data = [{"LoadBalancers": [{
        "LoadBalancerName": "lb-skip",
        "LoadBalancerArn": "arn:aws:elasticloadbalancing:us-east-1:1:loadbalancer/app/lb-skip/x",
        "Type": "application",
        "Scheme": "internet-facing",
        "AvailabilityZones": [{"ZoneName": "us-east-1a"}],
    }]}]

    class FakeV2:
        def get_paginator(self, name):
            class P:
                def paginate(self):
                    return lbs_data
            return P()

        def describe_tags(self, ResourceArns=None):
            return {"TagDescriptions": []}

    class FakeClassic:
        def get_paginator(self, name):
            class P:
                def paginate(self):
                    return [{"LoadBalancerDescriptions": []}]
            return P()

    class FakeCW:
        def get_metric_statistics(self, **kwargs):
            return {"Datapoints": []}

    monkeypatch.setattr(elb_scanner, "_elbv2_client", FakeV2())
    monkeypatch.setattr(elb_scanner, "_elb_client", FakeClassic())
    monkeypatch.setattr(elb_scanner, "_cloudwatch_client", FakeCW())
    assert elb_scanner.scan_unused_elb() == []


def test_elb_gateway_skipped(monkeypatch):
    lbs_data = [{"LoadBalancers": [{
        "LoadBalancerName": "gw",
        "LoadBalancerArn": "arn:aws:elasticloadbalancing:us-east-1:1:loadbalancer/gwy/gw/y",
        "Type": "gateway",
        "Scheme": "internal",
        "AvailabilityZones": [{"ZoneName": "us-east-1a"}],
    }]}]

    class FakeV2:
        def get_paginator(self, name):
            class P:
                def paginate(self):
                    return lbs_data
            return P()

        def describe_tags(self, ResourceArns=None):
            return {"TagDescriptions": []}

    class FakeClassic:
        def get_paginator(self, name):
            class P:
                def paginate(self):
                    return [{"LoadBalancerDescriptions": []}]
            return P()

    class ExplodingCW:
        def get_metric_statistics(self, **kwargs):
            raise AssertionError("gateway must not query CloudWatch")

    monkeypatch.setattr(elb_scanner, "_elbv2_client", FakeV2())
    monkeypatch.setattr(elb_scanner, "_elb_client", FakeClassic())
    monkeypatch.setattr(elb_scanner, "_cloudwatch_client", ExplodingCW())
    assert elb_scanner.scan_unused_elb() == []


def test_live_failure_falls_back_to_static_without_poisoning(monkeypatch):
    monkeypatch.setenv("PRICING_MODE", "live")
    estimator._PRICE_CACHE.clear()

    def boom(*a, **k):
        raise RuntimeError("pricing down")

    monkeypatch.setattr(estimator, "_get_pricing_client", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    # Force all live fetchers to fail via client error.
    price = estimator.get_price("EBS", "us-east-1", "live", volume_type="gp3")
    assert price == pytest.approx(0.10)
    # Live key must NOT be cached as a success; static fallback key should be.
    assert not any(str(k).startswith("live:") for k in estimator._PRICE_CACHE)
    assert any(str(k).startswith("static:") for k in estimator._PRICE_CACHE)


def test_gateway_priced_at_alb_rate(monkeypatch):
    monkeypatch.setenv("PRICING_MODE", "static")
    estimator._PRICE_CACHE.clear()
    hourly_elb = estimator.get_price("ELB", "us-east-1", "static")
    hourly_gw = estimator.get_price("GATEWAY", "us-east-1", "static")
    assert hourly_gw == pytest.approx(hourly_elb)


def test_report_does_not_mutate_and_handles_zero_days():
    resources = [{"type": "EBS", "id": "vol-1", "monthly_cost": 5.0, "days_idle": 0,
                  "az": "us-east-1a", "region": "us-east-1", "tags": {}}]
    snapshot = json.loads(json.dumps(resources))
    out = build_report(resources, 5.0, [], [], [], {})
    assert resources == snapshot  # no age_tier mutation
    assert "<7d" in out  # days_idle=0 must be <7d, not 90d+


def test_report_truncates_large_tables():
    big = [{"type": "EBS", "id": f"vol-{i}", "monthly_cost": 1.0,
            "az": "us-east-1a", "region": "us-east-1", "tags": {}} for i in range(600)]
    out = build_report(big, 600.0, [], [], [], {})
    assert "vol-599" in out
    assert "more rows in full S3 archive" not in out


def test_tag_checker_empty_env_falls_back(monkeypatch):
    monkeypatch.setenv("REQUIRED_TAGS", "")
    violations = tag_checker.check_tag_compliance([{"type": "EBS", "id": "v", "tags": {}}])
    assert len(violations) == 1
    # None tags and list-format tags never crash.
    assert tag_checker.check_tag_compliance([{"type": "EBS", "id": "v", "tags": None}])
    assert tag_checker.check_tag_compliance(
        [{"type": "EBS", "id": "v", "tags": [{"Key": "owner", "Value": "x"}]}]) is not None


def test_waste_threshold_rejects_inf(monkeypatch):
    monkeypatch.setenv("WASTE_ALERT_THRESHOLD", "inf")
    assert lambda_handler._get_waste_alert_threshold() == lambda_handler.DEFAULT_WASTE_ALERT_THRESHOLD
    monkeypatch.setenv("WASTE_ALERT_THRESHOLD", "-5")
    assert lambda_handler._get_waste_alert_threshold() == lambda_handler.DEFAULT_WASTE_ALERT_THRESHOLD


def test_overall_status_failed_only_on_total_failure():
    assert lambda_handler._overall_status([], [], []) == "ok"
    assert lambda_handler._overall_status([{"stage": "x"}], [], [{"id": 1}]) == "partial"
    assert lambda_handler._overall_status([], [{"stage": "d"}], []) == "partial"
