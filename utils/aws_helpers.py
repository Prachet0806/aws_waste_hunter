# utils/aws_helpers.py
import logging
import re
import threading

from botocore.config import Config

logger = logging.getLogger(__name__)

# Boto3 config tuned for Lambda: bounded retries/timeouts so a hung
# dependency cannot burn the whole invocation. Adaptive retry with a
# modest read timeout; callers add their own overall timeout via futures.
BOTO3_CONFIG = Config(
    retries={"total_max_attempts": 3, "mode": "standard"},
    connect_timeout=5,
    read_timeout=10,
    max_pool_connections=20,
)

_REGION_RE = re.compile(r"^[a-z]{2}(-gov|-iso|-isob)?-[a-z]+-\d+$")


def resolve_region(explicit=None):
    """Resolve AWS region with safe fallbacks (never return empty string)."""
    import os

    region = explicit or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        return None
    region = str(region).strip()
    return region or None


def get_region_from_az(az):
    """Extract region from availability zone, handling Local Zones, Wavelength, and Outposts."""
    if not az or not isinstance(az, str):
        return None

    az = az.strip().lower()
    if not az:
        return None

    # Standard AZ:      us-east-1a            -> us-east-1
    # GovCloud/ISO:     us-gov-west-1a        -> us-gov-west-1
    # Local Zone:       us-east-1-bos-1a      -> us-east-1
    # Wavelength:       us-east-1-wl1-bos-wlz-1 -> us-east-1

    parts = az.split("-")

    # The region is everything up to and including the first segment that
    # begins with a digit (the region's numeric component). A trailing zone
    # letter on that segment (e.g. "1a") is stripped.
    region_parts = []
    for part in parts:
        region_parts.append(part)
        if part and part[0].isdigit():
            break
    else:
        # No numeric segment found: not a valid AZ.
        logger.debug(f"get_region_from_az: unrecognized AZ format: {az!r}")
        return None

    if len(region_parts) < 2:
        return None

    last = region_parts[-1]
    if last and last[0].isdigit() and last[-1].isalpha():
        region_parts[-1] = last[:-1]

    region = "-".join(region_parts)
    if not _REGION_RE.match(region):
        logger.debug(f"get_region_from_az: parsed invalid region {region!r} from {az!r}")
        return None
    return region


def safe_get_first(items, default=None):
    """Safely get first item from a list/tuple; return default otherwise."""
    if items is None:
        return default
    if isinstance(items, dict):
        logger.debug("safe_get_first called with dict, returning default")
        return default
    try:
        if isinstance(items, (list, tuple)):
            return items[0] if items else default
        # Generic iterable (not generator-consuming promise): try iterator.
        iterator = iter(items)
        try:
            return next(iterator)
        except StopIteration:
            return default
    except TypeError:
        return default


def chunk_list(items, size):
    """Split list into chunks of specified size."""
    if items is None:
        raise ValueError("chunk_list: items must not be None")
    if size is None or not isinstance(size, int) or size <= 0:
        raise ValueError(f"chunk_list: size must be a positive int, got {size!r}")
    if not hasattr(items, "__len__") or not hasattr(items, "__getitem__"):
        raise ValueError("chunk_list: items must be a sized sliceable sequence")
    for i in range(0, len(items), size):
        yield items[i : i + size]


def normalize_tags(raw_tags, source=""):
    """Normalize AWS tag representations to a plain {key: value} dict.

    Accepts dicts, [{Key,Value}], [{key,value}], [{k,v}] shapes.
    Never raises: malformed entries are skipped with a debug log.
    """
    if not raw_tags:
        return {}
    if isinstance(raw_tags, dict):
        out = {}
        for k, v in raw_tags.items():
            try:
                out[str(k)] = str(v) if v is not None else ""
            except Exception:
                logger.debug(f"normalize_tags: skipping bad entry in {source}")
        return out
    if isinstance(raw_tags, (list, tuple)):
        out = {}
        for t in raw_tags:
            if not isinstance(t, dict):
                continue
            key = t.get("Key", t.get("key", t.get("k")))
            value = t.get("Value", t.get("value", t.get("v", "")))
            if key is None:
                continue
            try:
                out[str(key)] = str(value) if value is not None else ""
            except Exception:
                continue
        return out
    logger.debug(f"normalize_tags: unexpected type {type(raw_tags)} in {source}")
    return {}


class NoMetricsCounter:
    """Compatibility facade over context-local state; production uses ScanContext."""
    def record(self, resource_id, reason, exc=None):
        from utils.scan_context import CURRENT
        context = CURRENT.get()
        if context:
            context.skip(resource_id, reason)
            if exc is not None:
                context.error(reason, exc)
        logger.warning("Skipped %s: %s", resource_id, reason)

    def count(self):
        from utils.scan_context import CURRENT
        context = CURRENT.get()
        return len(context.skips) if context else 0

    def ids(self):
        from utils.scan_context import CURRENT
        context = CURRENT.get()
        return [s['resource_id'] for s in context.skips] if context else []

    def reset(self):
        # A scanner must never reset another scanner's evidence.
        return None


GLOBAL_NO_METRICS = NoMetricsCounter()


def log_and_skip(resource_id, reason, exc=None):
    GLOBAL_NO_METRICS.record(resource_id, reason, exc)
    return None


def reset_no_metrics():
    return None
