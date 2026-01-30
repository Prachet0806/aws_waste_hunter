# utils/aws_helpers.py
import boto3
import logging
from botocore.config import Config

logger = logging.getLogger(__name__)

# Boto3 config with retries and timeouts
BOTO3_CONFIG = Config(
    retries={"max_attempts": 10, "mode": "adaptive"},
    connect_timeout=5,
    read_timeout=60,
)


def get_region_from_az(az):
    """Extract region from availability zone, handling Local Zones and Wavelength."""
    if not az:
        return None
    
    # Local Zone format: us-east-1-bos-1a (4+ parts)
    # Wavelength format: us-east-1-wl1-bos-wlz-1 (6+ parts)
    # Standard format: us-east-1a (3 parts with zone letter at end)
    
    parts = az.split("-")
    
    if len(parts) > 3:
        # Local Zone or Wavelength - region is first 3 parts
        return "-".join(parts[:3])
    elif len(parts) == 3:
        # Standard AZ - need to remove zone letter from last part
        # us-east-1a -> ["us", "east", "1a"] -> "us-east-1"
        last_part = parts[2]
        # Remove trailing letter if present (zone identifier)
        if last_part and last_part[-1].isalpha():
            last_part = last_part[:-1]
        return f"{parts[0]}-{parts[1]}-{last_part}"
    else:
        # Malformed or too short
        return az


def safe_get_first(items, default=None):
    """Safely get first item from list."""
    return items[0] if items else default


def chunk_list(items, size):
    """Split list into chunks of specified size."""
    for i in range(0, len(items), size):
        yield items[i : i + size]
