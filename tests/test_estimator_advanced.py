import pytest
import time
from cost_engine import estimator


class TestDeduplication:
    """Test resource deduplication in estimator."""

    def test_deduplicates_same_resources(self, monkeypatch):
        """Test that duplicate resources are removed."""
        monkeypatch.setenv("PRICING_MODE", "static")
        estimator._PRICE_CACHE.clear()
        
        resources = [
            {"type": "EBS", "id": "vol-1", "size_gb": 10},
            {"type": "EBS", "id": "vol-1", "size_gb": 10},  # Duplicate
            {"type": "EC2", "id": "i-1", "instance_type": "t3.micro"},
        ]

        estimated, total = estimator.estimate_monthly_waste(resources)

        # Should only have 2 unique resources
        assert len(estimated) == 2
        ids = [(r["type"], r["id"]) for r in estimated]
        assert ("EBS", "vol-1") in ids
        assert ("EC2", "i-1") in ids

    def test_keeps_different_resources(self, monkeypatch):
        """Test that different resources are not deduplicated."""
        monkeypatch.setenv("PRICING_MODE", "static")
        estimator._PRICE_CACHE.clear()
        
        resources = [
            {"type": "EBS", "id": "vol-1", "size_gb": 10},
            {"type": "EBS", "id": "vol-2", "size_gb": 20},
            {"type": "EC2", "id": "i-1", "instance_type": "t3.micro"},
        ]

        estimated, total = estimator.estimate_monthly_waste(resources)

        assert len(estimated) == 3


class TestNoMutation:
    """Test that estimator doesn't mutate input."""

    def test_doesnt_mutate_input(self, monkeypatch):
        """Test that input resources are not modified."""
        monkeypatch.setenv("PRICING_MODE", "static")
        estimator._PRICE_CACHE.clear()
        
        resources = [
            {"type": "EBS", "id": "vol-1", "size_gb": 10},
        ]
        
        original_resource = resources[0].copy()

        estimated, total = estimator.estimate_monthly_waste(resources)

        # Original resource should not have monthly_cost
        assert "monthly_cost" not in resources[0]
        assert resources[0] == original_resource


class TestPricingModeValidation:
    """Test pricing mode validation."""

    def test_invalid_pricing_mode_fallback(self, monkeypatch):
        """Test invalid pricing mode falls back to static."""
        monkeypatch.setenv("PRICING_MODE", "invalid_mode")
        estimator._PRICE_CACHE.clear()
        
        resources = [
            {"type": "EBS", "id": "vol-1", "size_gb": 10},
        ]

        estimated, total = estimator.estimate_monthly_waste(resources)

        # Should complete without error, using static pricing
        assert len(estimated) == 1
        assert estimated[0]["monthly_cost"] > 0

    def test_static_pricing_mode(self, monkeypatch):
        """Test explicit static pricing mode."""
        monkeypatch.setenv("PRICING_MODE", "static")
        estimator._PRICE_CACHE.clear()
        
        resources = [
            {"type": "EBS", "id": "vol-1", "size_gb": 10},
        ]

        estimated, total = estimator.estimate_monthly_waste(resources)

        assert len(estimated) == 1
        # Static pricing: $0.10 per GB-month * 10 GB = $1.00
        assert estimated[0]["monthly_cost"] == 1.0


class TestCacheTTL:
    """Test pricing cache TTL functionality."""

    def test_cache_ttl_expiration(self, monkeypatch):
        """Test that cache entries expire after TTL."""
        monkeypatch.setenv("PRICING_MODE", "live")
        estimator._PRICE_CACHE.clear()
        estimator.CACHE_TTL = 1  # 1 second for testing
        
        # Add a cache entry
        key = ("EC2", "t3.micro", "us-east-1")
        estimator._PRICE_CACHE[key] = {"price": 0.01, "timestamp": time.time()}
        
        # Should be in cache
        assert key in estimator._PRICE_CACHE
        cached = estimator._PRICE_CACHE[key]
        assert time.time() - cached["timestamp"] < estimator.CACHE_TTL
        
        # Wait for TTL to expire
        time.sleep(1.1)
        
        # Trigger cache clean
        estimator._clean_cache()
        
        # Should be removed
        assert key not in estimator._PRICE_CACHE

    def test_cache_size_limit(self, monkeypatch):
        """Test that cache enforces size limit."""
        estimator._PRICE_CACHE.clear()
        estimator.MAX_CACHE_SIZE = 10
        
        # Add more entries than the limit
        for i in range(15):
            key = ("TEST", f"instance-{i}", "region")
            estimator._PRICE_CACHE[key] = {"price": i, "timestamp": time.time()}
        
        # Trigger cleanup
        estimator._clean_cache()
        
        # Cache size should be at or below limit
        assert len(estimator._PRICE_CACHE) <= estimator.MAX_CACHE_SIZE


class TestFailedLookups:
    """Test handling of failed pricing lookups."""

    def test_failed_lookup_not_cached(self, monkeypatch):
        """Test that None results are not cached."""
        estimator._PRICE_CACHE.clear()
        
        # Mock a function that returns None
        def mock_get_price(instance_type, region):
            return None
        
        monkeypatch.setattr(estimator, "_get_ec2_hourly_price", mock_get_price)
        
        # Try to get price (will return None)
        result = estimator._safe_price(mock_get_price, "t3.micro", "us-east-1")
        
        assert result is None
        # Should not be in cache
        key = ("EC2", "t3.micro", "us-east-1")
        assert key not in estimator._PRICE_CACHE


class TestUnknownResourceTypes:
    """Test handling of unknown resource types."""

    def test_unknown_resource_type(self, monkeypatch):
        """Test that unknown types are handled gracefully."""
        monkeypatch.setenv("PRICING_MODE", "static")
        estimator._PRICE_CACHE.clear()
        
        resources = [
            {"type": "UNKNOWN", "id": "res-1"},
            {"type": "EBS", "id": "vol-1", "size_gb": 10},
        ]

        estimated, total = estimator.estimate_monthly_waste(resources)

        # Should not crash
        assert len(estimated) == 2
        # Unknown type should have 0 cost
        unknown = [r for r in estimated if r["type"] == "UNKNOWN"][0]
        assert unknown["monthly_cost"] == 0


class TestRDSTypes:
    """Test RDS cluster vs instance type handling."""

    def test_rds_cluster_type(self, monkeypatch):
        """Test RDS_CLUSTER type."""
        monkeypatch.setenv("PRICING_MODE", "static")
        estimator._PRICE_CACHE.clear()
        
        resources = [
            {"type": "RDS_CLUSTER", "id": "cluster-1"},
        ]

        estimated, total = estimator.estimate_monthly_waste(resources)

        assert len(estimated) == 1
        assert estimated[0]["monthly_cost"] == estimator.DEFAULT_PRICING["RDS"]

    def test_rds_instance_type(self, monkeypatch):
        """Test RDS_INSTANCE type."""
        monkeypatch.setenv("PRICING_MODE", "static")
        estimator._PRICE_CACHE.clear()
        
        resources = [
            {"type": "RDS_INSTANCE", "id": "instance-1"},
        ]

        estimated, total = estimator.estimate_monthly_waste(resources)

        assert len(estimated) == 1
        assert estimated[0]["monthly_cost"] == estimator.DEFAULT_PRICING["RDS"]


class TestTotalCalculation:
    """Test total cost calculation."""

    def test_total_sum(self, monkeypatch):
        """Test that total is sum of individual costs."""
        monkeypatch.setenv("PRICING_MODE", "static")
        estimator._PRICE_CACHE.clear()
        
        resources = [
            {"type": "EBS", "id": "vol-1", "size_gb": 10},  # $1.00
            {"type": "EBS", "id": "vol-2", "size_gb": 50},  # $5.00
            {"type": "EC2", "id": "i-1", "instance_type": "t3.micro"},  # $8.50
        ]

        estimated, total = estimator.estimate_monthly_waste(resources)

        expected_total = 1.0 + 5.0 + 8.50
        assert total == round(expected_total, 2)

    def test_empty_resources(self, monkeypatch):
        """Test with no resources."""
        monkeypatch.setenv("PRICING_MODE", "static")
        estimator._PRICE_CACHE.clear()
        
        resources = []

        estimated, total = estimator.estimate_monthly_waste(resources)

        assert len(estimated) == 0
        assert total == 0


class TestElbTypePricing:
    """Test that NLB/Classic ELBs use type-specific live pricing."""

    def test_nlb_uses_nlb_price(self, monkeypatch):
        """Test NLB uses _get_nlb_hourly_price in live mode."""
        monkeypatch.setenv("PRICING_MODE", "live")
        estimator._PRICE_CACHE.clear()
        monkeypatch.setattr(estimator, "_get_nlb_hourly_price", lambda region: 0.02)
        monkeypatch.setattr(estimator, "_get_alb_hourly_price", lambda region: 0.99)

        resources = [
            {"type": "ELB", "id": "nlb-1", "lb_type": "NETWORK", "region": "us-east-1"},
        ]

        estimated, total = estimator.estimate_monthly_waste(resources)

        assert estimated[0]["monthly_cost"] == round(0.02 * estimator.HOURS_PER_MONTH, 2)

    def test_classic_uses_classic_price(self, monkeypatch):
        """Test Classic ELB uses _get_classic_elb_hourly_price in live mode."""
        monkeypatch.setenv("PRICING_MODE", "live")
        estimator._PRICE_CACHE.clear()
        monkeypatch.setattr(estimator, "_get_classic_elb_hourly_price", lambda region: 0.03)
        monkeypatch.setattr(estimator, "_get_alb_hourly_price", lambda region: 0.99)

        resources = [
            {"type": "ELB", "id": "classic-1", "lb_type": "CLASSIC", "region": "us-east-1"},
        ]

        estimated, total = estimator.estimate_monthly_waste(resources)

        assert estimated[0]["monthly_cost"] == round(0.03 * estimator.HOURS_PER_MONTH, 2)

    def test_alb_uses_alb_price(self, monkeypatch):
        """Test ALB uses _get_alb_hourly_price in live mode."""
        monkeypatch.setenv("PRICING_MODE", "live")
        estimator._PRICE_CACHE.clear()
        monkeypatch.setattr(estimator, "_get_nlb_hourly_price", lambda region: 0.02)
        monkeypatch.setattr(estimator, "_get_alb_hourly_price", lambda region: 0.01)

        resources = [
            {"type": "ELB", "id": "alb-1", "lb_type": "APPLICATION", "region": "us-east-1"},
        ]

        estimated, total = estimator.estimate_monthly_waste(resources)

        assert estimated[0]["monthly_cost"] == round(0.01 * estimator.HOURS_PER_MONTH, 2)


class TestRdsInstancePricing:
    """Test RDS instance pricing by class."""

    def test_rds_instance_known_class(self, monkeypatch):
        """Test known instance class uses class-specific price."""
        monkeypatch.setenv("PRICING_MODE", "static")
        estimator._PRICE_CACHE.clear()

        resources = [
            {"type": "RDS_INSTANCE", "id": "instance-1", "instance_class": "db.t3.small"},
        ]

        estimated, total = estimator.estimate_monthly_waste(resources)

        assert estimated[0]["monthly_cost"] == estimator.DEFAULT_PRICING["RDS_INSTANCE"]["db.t3.small"]

    def test_rds_instance_unknown_class_falls_back(self, monkeypatch):
        """Test unknown instance class falls back to cluster default."""
        monkeypatch.setenv("PRICING_MODE", "static")
        estimator._PRICE_CACHE.clear()

        resources = [
            {"type": "RDS_INSTANCE", "id": "instance-1", "instance_class": "db.x1e.32xlarge"},
        ]

        estimated, total = estimator.estimate_monthly_waste(resources)

        assert estimated[0]["monthly_cost"] == estimator.DEFAULT_PRICING["RDS"]


class TestLivePricingFilters:
    """Test live pricing lookup details."""

    def test_ebs_uses_volume_api_name_filter(self, monkeypatch):
        """Test EBS live lookup filters on volumeApiName to distinguish gp2/gp3."""
        captured_calls = []

        class FakePricing:
            def get_products(self, **kwargs):
                captured_calls.append(kwargs["Filters"])
                return {"PriceList": []}

        monkeypatch.setattr(estimator, "_get_pricing_client", lambda: FakePricing())
        estimator._PRICE_CACHE.clear()

        result = estimator._get_ebs_gb_month_price("gp3", "us-east-1")

        assert result is None  # empty PriceList -> no price -> fallback in estimator
        # First call should use the precise volumeApiName filter
        first_filters = {f["Field"]: f["Value"] for f in captured_calls[0]}
        assert first_filters["volumeApiName"] == "gp3"

    def test_ec2_unknown_type_uses_fallback_constant(self, monkeypatch):
        """Test unknown EC2 instance types use DEFAULT_EC2_FALLBACK."""
        monkeypatch.setenv("PRICING_MODE", "static")
        estimator._PRICE_CACHE.clear()

        resources = [
            {"type": "EC2", "id": "i-1", "instance_type": "x2gd.metal"},
        ]

        estimated, total = estimator.estimate_monthly_waste(resources)

        assert estimated[0]["monthly_cost"] == estimator.DEFAULT_EC2_FALLBACK


class TestPricingOverrideMerge:
    """Test partial PRICING_JSON overrides deep-merge over defaults."""

    def test_partial_override_keeps_other_keys(self, monkeypatch):
        """Test partial override doesn't wipe untouched keys."""
        import json

        monkeypatch.setenv("PRICING_JSON", json.dumps({"EBS": 1.0}))
        estimator._PRICE_CACHE.clear()

        resources = [
            {"type": "EC2", "id": "i-1", "instance_type": "t3.micro"},
        ]

        estimated, total = estimator.estimate_monthly_waste(resources)

        # EC2 default still present -> $8.50/mo
        assert estimated[0]["monthly_cost"] == 8.50

    def test_invalid_json_falls_back_to_defaults(self, monkeypatch):
        """Test malformed PRICING_JSON falls back to defaults without crashing."""
        monkeypatch.setenv("PRICING_JSON", "{not valid json")
        estimator._PRICE_CACHE.clear()

        resources = [
            {"type": "EBS", "id": "vol-1", "size_gb": 10},
        ]

        estimated, total = estimator.estimate_monthly_waste(resources)

        assert estimated[0]["monthly_cost"] == 1.0


class TestEipSnapshotPricing:
    """Test EIP and SNAPSHOT resource pricing (A1/A2)."""

    def test_eip_flat_price(self, monkeypatch):
        """Test unattached EIPs use flat monthly price."""
        monkeypatch.setenv("PRICING_MODE", "static")
        estimator._PRICE_CACHE.clear()

        resources = [
            {"type": "EIP", "id": "eip-1"},
        ]

        estimated, total = estimator.estimate_monthly_waste(resources)

        assert len(estimated) == 1
        assert estimated[0]["monthly_cost"] == estimator.DEFAULT_PRICING["EIP"]
        assert total == round(estimator.DEFAULT_PRICING["EIP"], 2)

    def test_snapshot_price_by_size(self, monkeypatch):
        """Test snapshot cost scales with size_gb."""
        monkeypatch.setenv("PRICING_MODE", "static")
        estimator._PRICE_CACHE.clear()

        resources = [
            {"type": "SNAPSHOT", "id": "snap-1", "size_gb": 100},
        ]

        estimated, total = estimator.estimate_monthly_waste(resources)

        expected = 100 * estimator.DEFAULT_PRICING["SNAPSHOT"]
        assert estimated[0]["monthly_cost"] == round(expected, 2)
        assert total == round(expected, 2)

    def test_snapshot_missing_size_defaults_zero(self, monkeypatch):
        """Test snapshot without size_gb costs nothing rather than crashing."""
        monkeypatch.setenv("PRICING_MODE", "static")
        estimator._PRICE_CACHE.clear()

        resources = [
            {"type": "SNAPSHOT", "id": "snap-1"},
        ]

        estimated, total = estimator.estimate_monthly_waste(resources)

        assert estimated[0]["monthly_cost"] == 0
