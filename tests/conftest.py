"""
Pytest configuration file.
Ensures project root is in Python path for imports and isolates global state.
"""
import sys
from pathlib import Path

# Add project root to Python path (guard against duplicates on re-import)
project_root = str(Path(__file__).parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import os
import pytest


def _reset_scanner_thresholds():
    """Reset lazily-cached scanner thresholds so env changes take effect."""
    try:
        import scanner.ec2_scanner as ec2_scanner
        ec2_scanner.CPU_THRESHOLD = None
        ec2_scanner.NETWORK_THRESHOLD = None
        if hasattr(ec2_scanner, "_no_metrics_ids"):
            ec2_scanner.reset_no_metrics_state()
    except Exception:
        pass


def _clear_price_cache():
    try:
        import cost_engine.estimator as estimator
        estimator.clear_price_cache()
        estimator.CACHE_TTL = 3600
        estimator.MAX_CACHE_SIZE = 1000
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_global_state(monkeypatch, request):
    """Auto-reset module globals and price cache between tests."""
    if request.node.get_closest_marker("staging"):
        yield
        return
    monkeypatch.setenv("ACCOUNT_ID", "123456789012")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "audit-dummy")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "audit-dummy")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Unit tests may not access the network")))
    _reset_scanner_thresholds()
    _clear_price_cache()
    yield
    _reset_scanner_thresholds()
    _clear_price_cache()


@pytest.fixture
def static_pricing(monkeypatch):
    """Force static pricing mode for deterministic cost tests."""
    monkeypatch.setenv("PRICING_MODE", "static")
    return True


@pytest.fixture
def default_tags(monkeypatch):
    """Ensure default REQUIRED_TAGS (unset env) for compliance tests."""
    monkeypatch.delenv("REQUIRED_TAGS", raising=False)
    return True
