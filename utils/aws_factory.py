# utils/aws_factory.py
"""Central boto3 client factory (Phase 4.2).

Replaces per-module lazy-init duplication with one region-resolving,
BOTO3_CONFIG-applying factory.
"""
import logging
import os

import boto3

from utils.aws_helpers import BOTO3_CONFIG

logger = logging.getLogger(__name__)


def resolve_region(explicit=None):
    region = explicit or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if region:
        region = str(region).strip()
        if region:
            return region
    return None


def get_client(service, region=None):
    """Create a boto3 client for service with shared retry/timeout config."""
    resolved = resolve_region(region)
    if resolved is None:
        logger.debug(f"get_client({service}): no region configured, deferring to boto3 defaults")
    return boto3.client(service, region_name=resolved, config=BOTO3_CONFIG)


def client_region(client):
    """Best-effort region name for an already-created client."""
    try:
        return client.meta.region_name
    except Exception:
        return resolve_region()
