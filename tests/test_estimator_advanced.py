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
