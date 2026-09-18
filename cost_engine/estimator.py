import json
import os
import logging
import time

import boto3

from utils.aws_helpers import BOTO3_CONFIG

logger = logging.getLogger(__name__)

HOURS_PER_MONTH = 730
CACHE_TTL = 3600  # 1 hour cache TTL for live prices
STATIC_CACHE_TTL = 86400 * 7  # 7 days for static fallback entries
MAX_CACHE_SIZE = 1000  # Maximum cache entries
DEFAULT_EC2_FALLBACK = 50.0  # $/month fallback for unknown EC2 instance types

PRICING_REGION_MAP = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "ca-central-1": "Canada (Central)",
    "ca-west-1": "Canada West (Calgary)",
    "eu-west-1": "EU (Ireland)",
    "eu-west-2": "EU (London)",
    "eu-west-3": "EU (Paris)",
    "eu-central-1": "EU (Frankfurt)",
    "eu-central-2": "EU (Zurich)",
    "eu-north-1": "EU (Stockholm)",
    "eu-south-1": "EU (Milan)",
    "eu-south-2": "EU (Spain)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-south-2": "Asia Pacific (Hyderabad)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-southeast-3": "Asia Pacific (Jakarta)",
    "ap-southeast-4": "Asia Pacific (Melbourne)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "ap-northeast-3": "Asia Pacific (Osaka)",
    "ap-east-1": "Asia Pacific (Hong Kong)",
    "sa-east-1": "South America (Sao Paulo)",
    "af-south-1": "Africa (Cape Town)",
    "me-south-1": "Middle East (Bahrain)",
    "me-central-1": "Middle East (UAE)",
    "il-central-1": "Israel (Tel Aviv)",
}

EBS_VOLUME_TYPE_MAP = {
    "gp2": "General Purpose",
    "gp3": "General Purpose",
    "gp": "General Purpose",
    "io1": "Provisioned IOPS",
    "io2": "Provisioned IOPS",
    "st1": "Throughput Optimized HDD",
    "sc1": "Cold HDD",
    "standard": "Magnetic",
}

_PRICE_CACHE = {}
_pricing_client = None

# Small pricing snapshot. Values are approximate; override via PRICING_JSON/PRICING_FILE.
# Live Pricing API is primary when PRICING_MODE=live; these are the cached fallback.
DEFAULT_PRICING = {
    "EBS": 0.10,        # $ per GB-month (gp3 approx)
    "EC2": {
        "t3.micro": 8.50,
        "t3.small": 17.00,
        "t3.medium": 34.00
    },
    "ELB": 18.00,       # $ per ALB-month (approx)
    "NLB": 18.00,
    "ELB_CLASSIC": 18.00,
    "RDS": 120.00,      # $ per stopped cluster (approx)
    "RDS_INSTANCE": {   # $ per stopped/idle instance by class
        "db.t3.micro": 13.00,
        "db.t3.small": 26.00,
        "db.t3.medium": 52.00,
        "db.t4g.micro": 12.00,
        "db.t4g.small": 25.00,
        "db.r5.large": 200.00,
    },
    "EIP": 3.60,        # $ per unattached Elastic IP-month (approx)
    "SNAPSHOT": 0.05,   # $ per GB-month of EBS snapshot storage (approx)
}

_REQUIRED_PRICING_KEYS = ("EBS", "EC2", "ELB", "RDS", "RDS_INSTANCE", "EIP", "SNAPSHOT")


def _field(resource, name, default=None):
    """Read a field from top-level or details (normalized schema)."""
    if not isinstance(resource, dict):
        return default
    if resource.get(name) is not None:
        return resource.get(name)
    details = resource.get("details")
    if isinstance(details, dict) and details.get(name) is not None:
        return details.get(name)
    return default


def _load_pricing():
    """Load pricing config, deep-merging any user override over the defaults."""
    loaded = None

    pricing_json = os.environ.get("PRICING_JSON")
    if pricing_json:
        try:
            loaded = json.loads(pricing_json)
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid PRICING_JSON, using defaults: {e}")

    if loaded is None:
        pricing_file = os.environ.get("PRICING_FILE")
        if pricing_file:
            if os.path.exists(pricing_file):
                try:
                    with open(pricing_file, "r", encoding="utf-8") as handle:
                        loaded = json.load(handle)
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f"Invalid PRICING_FILE, using defaults: {e}")
            else:
                logger.warning(f"PRICING_FILE not found: {pricing_file}, using defaults")

    if loaded is None:
        return DEFAULT_PRICING

    if not isinstance(loaded, dict):
        logger.warning("PRICING_JSON/PRICING_FILE must be a JSON object, using defaults")
        return DEFAULT_PRICING

    # Deep merge over defaults so partial configs don't cause KeyErrors
    merged = json.loads(json.dumps(DEFAULT_PRICING))
    for key, value in loaded.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key].update(value)
        else:
            merged[key] = value

    # Warn about required keys missing from the USER override (not merged).
    for key in _REQUIRED_PRICING_KEYS:
        if key not in loaded:
            logger.warning(f"Pricing override missing '{key}', using default")

    return merged


def _get_pricing_client():
    """Lazy initialization of Pricing API client (always us-east-1)."""
    global _pricing_client
    if _pricing_client is None:
        _pricing_client = boto3.client("pricing", region_name="us-east-1", config=BOTO3_CONFIG)
    return _pricing_client


def _clean_cache():
    """Remove expired cache entries and enforce size limit (compat for tests)."""
    global _PRICE_CACHE
    now = time.time()

    # Remove expired entries (support per-entry TTL).
    expired = []
    for k, v in _PRICE_CACHE.items():
        ttl = v.get("ttl", CACHE_TTL)
        if now - v.get("timestamp", 0) > ttl:
            expired.append(k)
    for k in expired:
        del _PRICE_CACHE[k]

    # Enforce size limit (remove oldest entries)
    if len(_PRICE_CACHE) > MAX_CACHE_SIZE:
        sorted_items = sorted(_PRICE_CACHE.items(), key=lambda x: x[1].get("timestamp", 0))
        for k, _ in sorted_items[:len(_PRICE_CACHE) - MAX_CACHE_SIZE]:
            del _PRICE_CACHE[k]


def clear_price_cache():
    """Explicit cache reset (tests/teardown should call this, not prod paths)."""
    _PRICE_CACHE.clear()


def _safe_price(fn, *args):
    """Safe wrapper for pricing API calls: returns None on any failure."""
    try:
        return fn(*args)
    except Exception as e:
        logger.warning(f"Pricing API call failed: {e}")
        return None


def _get_cached_price(key):
    """Get price from cache if valid TTL (compat: checks namespaced + legacy keys)."""
    for candidate in (key, f"live:{key}", f"static:{key}"):
        if candidate in _PRICE_CACHE:
            cached = _PRICE_CACHE[candidate]
            ttl = cached.get("ttl", CACHE_TTL)
            if time.time() - cached.get("timestamp", 0) < ttl:
                return cached.get("price")
    # Also prune expired entries on read.
    _clean_cache()
    return None


def _cache_price(key, price, ttl=None, namespace=None):
    """Cache a price lookup. Namespace separates live vs static (Decision 2)."""
    if price is None:
        return
    _clean_cache()
    store_key = f"{namespace}:{key}" if namespace else key
    _PRICE_CACHE[store_key] = {
        "price": price,
        "timestamp": time.time(),
        "ttl": ttl or CACHE_TTL,
    }


def _get_static_fallback(resource_type, **kwargs):
    """Get static fallback price from merged pricing config.

    Returns prices in canonical units:
    - EC2, ELB, NLB, ELB_CLASSIC: hourly
    - EBS, SNAPSHOT: per-GB-month
    - RDS, RDS_CLUSTER, RDS_INSTANCE, EIP: monthly
    """
    pricing = _load_pricing()
    if not isinstance(pricing, dict):
        return None

    if resource_type == "EC2":
        instance_type = kwargs.get("instance_type")
        ec2_table = pricing.get("EC2")
        if not isinstance(ec2_table, dict):
            return DEFAULT_EC2_FALLBACK / HOURS_PER_MONTH
        monthly = ec2_table.get(instance_type, DEFAULT_EC2_FALLBACK)
        try:
            return float(monthly) / HOURS_PER_MONTH
        except (TypeError, ValueError):
            return DEFAULT_EC2_FALLBACK / HOURS_PER_MONTH

    elif resource_type == "EBS":
        try:
            return float(pricing.get("EBS", 0.10))
        except (TypeError, ValueError):
            return 0.10

    elif resource_type in ("ELB", "NLB", "ELB_CLASSIC"):
        raw = pricing.get(resource_type, pricing.get("ELB", 18.00))
        try:
            return float(raw) / HOURS_PER_MONTH
        except (TypeError, ValueError):
            return 18.00 / HOURS_PER_MONTH

    elif resource_type in ("RDS", "RDS_CLUSTER"):
        try:
            return float(pricing.get("RDS", 120.00))
        except (TypeError, ValueError):
            return 120.00

    elif resource_type == "RDS_INSTANCE":
        instance_class = kwargs.get("instance_class")
        table = pricing.get("RDS_INSTANCE")
        default = pricing.get("RDS", 120.00)
        if not isinstance(table, dict):
            try:
                return float(default)
            except (TypeError, ValueError):
                return 120.00
        try:
            return float(table.get(instance_class, default))
        except (TypeError, ValueError):
            return 120.00

    elif resource_type == "EIP":
        try:
            return float(pricing.get("EIP", 3.60))
        except (TypeError, ValueError):
            return 3.60

    elif resource_type == "SNAPSHOT":
        try:
            return float(pricing.get("SNAPSHOT", 0.05))
        except (TypeError, ValueError):
            return 0.05

    return None


def get_price(resource_type, region, pricing_mode=None, **kwargs):
    """
    Get price with fallback chain: live cache -> live API -> static cache -> static config.

    Decision 2: live is primary when PRICING_MODE=live; static is the cached
    fallback, stored under separate cache keys so a transient live failure
    never poisons future live lookups.

    Returns price per unit (hourly for EC2/ELB, per-GB-month for EBS/SNAPSHOT,
    monthly for RDS/EIP), or None for unknown resource types.
    """
    if pricing_mode is None:
        pricing_mode = os.environ.get("PRICING_MODE", "static").lower()

    if pricing_mode not in ["static", "live"]:
        logger.warning(f"Invalid PRICING_MODE '{pricing_mode}', using 'static'")
        pricing_mode = "static"

    region = region or "us-east-1"

    # Build cache key
    if resource_type == "EC2":
        key = ("EC2", kwargs.get("instance_type"), region)
    elif resource_type == "EBS":
        key = ("EBS", kwargs.get("volume_type"), region)
    elif resource_type in ("ELB", "NLB", "ELB_CLASSIC", "GATEWAY"):
        key = (resource_type, region)
    elif resource_type in ("RDS", "RDS_CLUSTER"):
        key = ("RDS", region)
    elif resource_type == "RDS_INSTANCE":
        key = ("RDS_INSTANCE", kwargs.get("instance_class"), region)
    elif resource_type == "EIP":
        key = ("EIP", region)
    elif resource_type == "SNAPSHOT":
        key = ("SNAPSHOT", region)
    else:
        logger.warning(f"Unknown resource type for pricing: {resource_type}")
        return None

    if pricing_mode == "live":
        # Step 1: live cache.
        live_key = f"live:{key}"
        if live_key in _PRICE_CACHE:
            cached = _PRICE_CACHE[live_key]
            if time.time() - cached.get("timestamp", 0) < CACHE_TTL:
                return cached.get("price")

        # Step 2: live API for all supported types.
        live_price = None
        try:
            if resource_type == "EC2":
                live_price = _safe_price(_get_ec2_hourly_price, kwargs.get("instance_type"), region)
            elif resource_type == "EBS":
                live_price = _safe_price(_get_ebs_gb_month_price, kwargs.get("volume_type"), region)
            elif resource_type == "ELB":
                live_price = _safe_price(_get_alb_hourly_price, region)
            elif resource_type == "NLB":
                live_price = _safe_price(_get_nlb_hourly_price, region)
            elif resource_type in ("ELB_CLASSIC", "GATEWAY"):
                live_price = _safe_price(_get_classic_elb_hourly_price, region)
            elif resource_type in ("RDS", "RDS_CLUSTER"):
                live_price = _safe_price(_get_rds_monthly_price, kwargs.get("instance_class"), kwargs.get("engine"), region)
            elif resource_type == "RDS_INSTANCE":
                live_price = _safe_price(_get_rds_monthly_price, kwargs.get("instance_class"), kwargs.get("engine"), region)
            elif resource_type == "EIP":
                live_price = _safe_price(_get_eip_monthly_price, region)
            elif resource_type == "SNAPSHOT":
                live_price = _safe_price(_get_snapshot_gb_month_price, region)
        except Exception as e:
            logger.warning(f"Live pricing API error for {resource_type}: {e}")

        if live_price is not None:
            _cache_price(key, live_price, ttl=CACHE_TTL, namespace="live")
            return live_price
        logger.info(f"Live pricing unavailable for {resource_type}, falling back to static")

    # Static path (also used in static mode): static cache -> config.
    static_key = f"static:{key}"
    if static_key in _PRICE_CACHE:
        cached = _PRICE_CACHE[static_key]
        if time.time() - cached.get("timestamp", 0) < STATIC_CACHE_TTL:
            return cached.get("price")

    if resource_type == "GATEWAY":
        logger.warning("Gateway LB has no dedicated price; using ALB rate as approximation")
        resource_type = "ELB"

    static_price = _get_static_fallback(resource_type, **kwargs)
    if static_price is not None:
        _cache_price(key, static_price, ttl=STATIC_CACHE_TTL, namespace="static")
        return static_price

    # Step 4: Hardcoded absolute defaults (last resort)
    hardcoded = {
        "EC2": DEFAULT_EC2_FALLBACK / HOURS_PER_MONTH,
        "EBS": 0.10,
        "ELB": 18.00 / HOURS_PER_MONTH,
        "NLB": 18.00 / HOURS_PER_MONTH,
        "ELB_CLASSIC": 18.00 / HOURS_PER_MONTH,
        "GATEWAY": 18.00 / HOURS_PER_MONTH,
        "RDS": 120.0,
        "RDS_CLUSTER": 120.0,
        "RDS_INSTANCE": 120.0,
        "EIP": 3.60,
        "SNAPSHOT": 0.05,
    }
    return hardcoded.get(resource_type)


def _get_location(region):
    if not region:
        return None
    location = PRICING_REGION_MAP.get(region)
    if location is None:
        logger.warning(f"Region {region!r} not in PRICING_REGION_MAP; Pricing API lookup may fail, using raw value")
        return region
    return location

_USAGE_UNITS = ("Hrs", "GB-Mo", "GB-Month", "GB-Months")


def _extract_price_per_unit(pricing_response):
    """Extract a USD price per usage unit, preferring hourly/per-GB dimensions.

    For multi-dimension offers (e.g. ALB hourly + LCU), only the hourly
    ('Hrs') dimension is accepted here; LCU/request dimensions return None so
    callers fall back to static instead of mispricing. Generic fallback to the
    first USD dimension applies only to single-dimension offers.
    """
    items = (pricing_response or {}).get("PriceList", []) or []
    for item in items:
        try:
            data = json.loads(item) if isinstance(item, str) else item
        except (json.JSONDecodeError, TypeError):
            continue
        terms = (data.get("terms", {}) or {}).get("OnDemand", {}) or {}
        for offer in terms.values():
            dimensions = (offer or {}).get("priceDimensions", {}) or {}
            hourly = [
                d for d in dimensions.values()
                if isinstance(d, dict) and d.get("unit") == "Hrs"
                and (d.get("pricePerUnit", {}) or {}).get("USD") is not None
            ]
            if hourly:
                try:
                    return float(hourly[0]["pricePerUnit"]["USD"])
                except (TypeError, ValueError, KeyError):
                    continue
            gb = [
                d for d in dimensions.values()
                if isinstance(d, dict) and d.get("unit") in ("GB-Mo", "GB-Month", "GB-Months")
                and (d.get("pricePerUnit", {}) or {}).get("USD") is not None
            ]
            if gb:
                try:
                    return float(gb[0]["pricePerUnit"]["USD"])
                except (TypeError, ValueError, KeyError):
                    continue
            if len(dimensions) == 1:
                dim = next(iter(dimensions.values()))
                price_str = (dim.get("pricePerUnit", {}) or {}).get("USD")
                if price_str is not None:
                    try:
                        return float(price_str)
                    except (TypeError, ValueError):
                        continue
    return None


def _query_products(service_code, filters, max_pages=3):
    """Query Pricing get_products with NextToken pagination (bounded)."""
    pricing = _get_pricing_client()
    collected = {"PriceList": []}
    token = None
    for _ in range(max_pages):
        kwargs = {"ServiceCode": service_code, "Filters": filters, "MaxResults": 10}
        if token:
            kwargs["NextToken"] = token
        response = pricing.get_products(**kwargs)
        collected["PriceList"].extend(response.get("PriceList", []) or [])
        token = response.get("NextToken")
        if not token or collected["PriceList"]:
            break
    return collected


def _get_ec2_hourly_price(instance_type, region):
    """Get EC2 hourly price from Pricing API (Linux/Shared baseline, logged)."""
    logger.debug(f"EC2 live price uses Linux/Shared baseline for {instance_type}; non-Linux may be underestimated")
    location = _get_location(region)
    if not location or not instance_type:
        return None

    pricing = _get_pricing_client()
    try:
        response = pricing.get_products(
            ServiceCode="AmazonEC2",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
                {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
                {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
                {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
                {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
                {"Type": "TERM_MATCH", "Field": "licenseModel", "Value": "No License required"},
            ],
            MaxResults=1,
        )
        return _extract_price_per_unit(response)
    except Exception as e:
        logger.error(f"Error fetching EC2 price for {instance_type} in {region}: {e}")
        return None


def _get_ebs_gb_month_price(volume_type, region):
    """Get EBS per-GB-month price from Pricing API."""
    location = _get_location(region)
    if not location:
        return None

    volume_api_name = volume_type or "gp3"

    try:
        # Prefer the precise volumeApiName attribute (e.g. "gp3") so gp2/gp3/io1
        # are not all conflated into a single family price.
        response = _query_products(
            "AmazonEC2",
            [
                {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
                {"Type": "TERM_MATCH", "Field": "volumeApiName", "Value": volume_api_name},
            ],
        )
        price = _extract_price_per_unit(response)

        # Fall back to the family-level filter for unusual/legacy volume types
        if price is None:
            volume_family = EBS_VOLUME_TYPE_MAP.get(volume_type, "General Purpose")
            response = _query_products(
                "AmazonEC2",
                [
                    {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                    {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
                    {"Type": "TERM_MATCH", "Field": "volumeType", "Value": volume_family},
                ],
            )
            price = _extract_price_per_unit(response)

        return price
    except Exception as e:
        logger.error(f"Error fetching EBS price for {volume_type} in {region}: {e}")
        return None


def _get_alb_hourly_price(region):
    """Get ALB hourly price from Pricing API."""
    location = _get_location(region)
    if not location:
        return None

    try:
        response = _query_products(
            "AWSELB",
            [
                {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Load Balancer"},
                {"Type": "TERM_MATCH", "Field": "loadBalancerType", "Value": "Application"},
                {"Type": "TERM_MATCH", "Field": "operation", "Value": "LoadBalancing:Application"},
            ],
        )
        return _extract_price_per_unit(response)
    except Exception as e:
        logger.error(f"Error fetching ELB price for {region}: {e}")
        return None


def _get_nlb_hourly_price(region):
    """Get NLB hourly price from Pricing API."""
    location = _get_location(region)
    if not location:
        return None

    try:
        response = _query_products(
            "AWSELB",
            [
                {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Load Balancer"},
                {"Type": "TERM_MATCH", "Field": "loadBalancerType", "Value": "Network"},
                {"Type": "TERM_MATCH", "Field": "operation", "Value": "LoadBalancing:Network"},
            ],
        )
        return _extract_price_per_unit(response)
    except Exception as e:
        logger.error(f"Error fetching NLB price for {region}: {e}")
        return None


def _get_classic_elb_hourly_price(region):
    """Get Classic ELB hourly price from Pricing API."""
    location = _get_location(region)
    if not location:
        return None

    try:
        response = _query_products(
            "AWSELB",
            [
                {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Load Balancer"},
                {"Type": "TERM_MATCH", "Field": "loadBalancerType", "Value": "Classic"},
                {"Type": "TERM_MATCH", "Field": "operation", "Value": "LoadBalancing:Classic"},
            ],
        )
        return _extract_price_per_unit(response)
    except Exception as e:
        logger.error(f"Error fetching Classic ELB price for {region}: {e}")
        return None


def _get_rds_monthly_price(instance_class, engine, region):
    """Get RDS monthly price from Pricing API (live primary, Decision 2)."""
    location = _get_location(region)
    if not location:
        return None
    filters = [{"Type": "TERM_MATCH", "Field": "location", "Value": location}]
    if instance_class:
        filters.append({"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_class})
    else:
        filters.append({"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Database Instance"})
    if engine:
        engine_map = {"aurora-mysql": "Aurora MySQL", "aurora-postgresql": "Aurora PostgreSQL",
                      "mysql": "MySQL", "postgres": "PostgreSQL", "mariadb": "MariaDB",
                      "oracle-ee": "Oracle", "sqlserver-ee": "SQL Server"}
        mapped = engine_map.get(str(engine).lower())
        if mapped:
            filters.append({"Type": "TERM_MATCH", "Field": "databaseEngine", "Value": mapped})
    try:
        response = _query_products("AmazonRDS", filters)
        price_hourly = _extract_price_per_unit(response)
        if price_hourly is None:
            return None
        # RDS Pricing API returns hourly; convert to monthly.
        return float(price_hourly) * HOURS_PER_MONTH
    except Exception as e:
        logger.error(f"Error fetching RDS price for {instance_class} in {region}: {e}")
        return None


def _get_eip_monthly_price(region):
    """Get EIP monthly price from Pricing API."""
    location = _get_location(region)
    if not location:
        return None
    try:
        response = _query_products(
            "AmazonEC2",
            [
                {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "IP Address"},
            ],
        )
        price_hourly = _extract_price_per_unit(response)
        if price_hourly is None:
            return None
        return float(price_hourly) * HOURS_PER_MONTH
    except Exception as e:
        logger.error(f"Error fetching EIP price for {region}: {e}")
        return None


def _get_snapshot_gb_month_price(region):
    """Get EBS snapshot per-GB-month price from Pricing API."""
    location = _get_location(region)
    if not location:
        return None
    try:
        response = _query_products(
            "AmazonEC2",
            [
                {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage Snapshot"},
            ],
        )
        return _extract_price_per_unit(response)
    except Exception as e:
        logger.error(f"Error fetching snapshot price for {region}: {e}")
        return None


def estimate_monthly_waste(resources):
    """
    Estimate monthly waste cost for resources.
    Returns new list with cost annotations, does not mutate input.
    Never clears the price cache (callers/tests manage lifecycle explicitly).
    """
    pricing_mode = os.environ.get("PRICING_MODE", "static").lower()

    # Validate pricing mode
    if pricing_mode not in ["static", "live"]:
        logger.warning(f"Invalid PRICING_MODE '{pricing_mode}', using 'static'")
        pricing_mode = "static"

    report = []
    total = 0

    # Deduplicate resources by (type, id)
    seen = set()
    unique_resources = []
    for r in resources or []:
        if not isinstance(r, dict):
            logger.warning(f"Skipping non-dict resource: {r!r}")
            continue
        key = (r.get("type"), r.get("id"))
        if key not in seen:
            seen.add(key)
            unique_resources.append(r)
        else:
            logger.warning(f"Duplicate resource detected: {key}")

    logger.info(f"Estimating costs for {len(unique_resources)} unique resources (pricing_mode={pricing_mode})")

    for r in unique_resources:
        cost = 0
        resource_type = (r.get("type") or "UNKNOWN")
        region = r.get("region") or (r.get("details", {}) or {}).get("region") or "us-east-1"
        if not r.get("region"):
            logger.debug(f"Resource {r.get('id')} missing region, defaulting to us-east-1")

        if resource_type == "EBS":
            volume_type = _field(r, "volume_type", "gp3")
            try:
                size = float(_field(r, "size_gb", 0) or 0)
            except (TypeError, ValueError):
                size = 0
            price_per_gb = get_price("EBS", region, pricing_mode, volume_type=volume_type)
            cost = size * (price_per_gb if price_per_gb is not None else 0.10)

        elif resource_type == "EC2":
            hourly = get_price("EC2", region, pricing_mode, instance_type=_field(r, "instance_type"))
            cost = hourly * HOURS_PER_MONTH if hourly is not None else 0

        elif resource_type == "ELB":
            lb_type = str(_field(r, "lb_type", "APPLICATION") or "APPLICATION").upper()
            if lb_type == "GATEWAY":
                logger.warning("Gateway LB priced at ALB rate (approximation)")
            price_type = {"NETWORK": "NLB", "CLASSIC": "ELB_CLASSIC", "GATEWAY": "ELB"}.get(lb_type, "ELB")
            hourly = get_price(price_type, region, pricing_mode)
            cost = hourly * HOURS_PER_MONTH if hourly is not None else 0

        elif resource_type in ("RDS_CLUSTER", "RDS"):
            # RDS prices are monthly (storage for stopped, full instance for idle)
            monthly = get_price("RDS", region, pricing_mode,
                                instance_class=_field(r, "instance_class"),
                                engine=_field(r, "engine"))
            cost = monthly if monthly is not None else 0

        elif resource_type == "RDS_INSTANCE":
            monthly = get_price("RDS_INSTANCE", region, pricing_mode,
                                instance_class=_field(r, "instance_class"),
                                engine=_field(r, "engine"))
            cost = monthly if monthly is not None else 0

        elif resource_type == "EIP":
            monthly = get_price("EIP", region, pricing_mode)
            cost = monthly if monthly is not None else 0

        elif resource_type == "SNAPSHOT":
            try:
                size = float(_field(r, "size_gb", 0) or 0)
            except (TypeError, ValueError):
                size = 0
            price_per_gb = get_price("SNAPSHOT", region, pricing_mode)
            cost = size * (price_per_gb if price_per_gb is not None else 0.05)

        else:
            logger.warning(f"Unknown resource type: {resource_type}")
            cost = 0

        # Create new dict with cost annotation (avoid mutation)
        annotated = {**r, "monthly_cost": round(cost, 2)}
        total += cost
        report.append(annotated)

    logger.info(f"Total estimated monthly waste: ${round(total, 2)}")
    return report, round(total, 2)
