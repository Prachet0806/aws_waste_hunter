import json
import os
import boto3
import logging
import time
from utils.aws_helpers import BOTO3_CONFIG

logger = logging.getLogger(__name__)

HOURS_PER_MONTH = 730
CACHE_TTL = 3600  # 1 hour cache TTL
MAX_CACHE_SIZE = 1000  # Maximum cache entries

PRICING_REGION_MAP = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "eu-west-1": "EU (Ireland)",
    "eu-west-2": "EU (London)",
    "eu-west-3": "EU (Paris)",
    "eu-central-1": "EU (Frankfurt)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "sa-east-1": "South America (Sao Paulo)",
    "ca-central-1": "Canada (Central)",
}

EBS_VOLUME_TYPE_MAP = {
    "gp2": "General Purpose",
    "gp3": "General Purpose",
    "io1": "Provisioned IOPS",
    "io2": "Provisioned IOPS",
    "st1": "Throughput Optimized HDD",
    "sc1": "Cold HDD",
    "standard": "Magnetic",
}

_PRICE_CACHE = {}
_pricing_client = None

# Very small pricing snapshot (can extend later)
DEFAULT_PRICING = {
    "EBS": 0.10,        # $ per GB-month (gp3 approx)
    "EC2": {
        "t3.micro": 8.50,
        "t3.small": 17.00,
        "t3.medium": 34.00
    },
    "ELB": 18.00,       # $ per ALB-month (approx)
    "RDS": 120.00       # $ per stopped cluster (approx)
}

def _load_pricing():
    pricing_json = os.environ.get("PRICING_JSON")
    if pricing_json:
        return json.loads(pricing_json)

    pricing_file = os.environ.get("PRICING_FILE")
    if pricing_file and os.path.exists(pricing_file):
        with open(pricing_file, "r", encoding="utf-8") as handle:
            return json.load(handle)

    return DEFAULT_PRICING

def _get_pricing_client():
    """Lazy initialization of Pricing API client."""
    global _pricing_client
    if _pricing_client is None:
        _pricing_client = boto3.client("pricing", region_name="us-east-1", config=BOTO3_CONFIG)
    return _pricing_client


def _clean_cache():
    """Remove expired cache entries and enforce size limit."""
    global _PRICE_CACHE
    now = time.time()
    
    # Remove expired entries
    expired = [k for k, v in _PRICE_CACHE.items() if now - v.get("timestamp", 0) > CACHE_TTL]
    for k in expired:
        del _PRICE_CACHE[k]
    
    # Enforce size limit (remove oldest entries)
    if len(_PRICE_CACHE) > MAX_CACHE_SIZE:
        sorted_items = sorted(_PRICE_CACHE.items(), key=lambda x: x[1].get("timestamp", 0))
        for k, _ in sorted_items[:len(_PRICE_CACHE) - MAX_CACHE_SIZE]:
            del _PRICE_CACHE[k]


def _safe_price(fn, *args):
    """Circuit breaker for pricing API calls with logging."""
    try:
        return fn(*args)
    except Exception as e:
        logger.warning(f"Pricing API call failed: {e}")
        return None

def _get_location(region):
    if not region:
        return None
    return PRICING_REGION_MAP.get(region, region)

def _extract_price_per_unit(pricing_response):
    for item in pricing_response.get("PriceList", []):
        data = json.loads(item)
        terms = data.get("terms", {}).get("OnDemand", {})
        for offer in terms.values():
            dimensions = offer.get("priceDimensions", {})
            for dim in dimensions.values():
                price_str = dim.get("pricePerUnit", {}).get("USD")
                if price_str is not None:
                    return float(price_str)
    return None

def _get_ec2_hourly_price(instance_type, region):
    """Get EC2 hourly price from Pricing API with TTL cache."""
    key = ("EC2", instance_type, region)
    
    # Check cache
    if key in _PRICE_CACHE:
        cached = _PRICE_CACHE[key]
        if time.time() - cached.get("timestamp", 0) < CACHE_TTL:
            return cached.get("price")
    
    _clean_cache()

    location = _get_location(region)
    if not location:
        logger.warning(f"Unknown region {region}, cannot fetch EC2 pricing")
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
        price = _extract_price_per_unit(response)
        
        # Only cache successful lookups
        if price is not None:
            _PRICE_CACHE[key] = {"price": price, "timestamp": time.time()}
        
        return price
    except Exception as e:
        logger.error(f"Error fetching EC2 price for {instance_type} in {region}: {e}")
        return None

def _get_ebs_gb_month_price(volume_type, region):
    """Get EBS per-GB-month price from Pricing API with TTL cache."""
    key = ("EBS", volume_type, region)
    
    # Check cache
    if key in _PRICE_CACHE:
        cached = _PRICE_CACHE[key]
        if time.time() - cached.get("timestamp", 0) < CACHE_TTL:
            return cached.get("price")
    
    _clean_cache()

    location = _get_location(region)
    if not location:
        logger.warning(f"Unknown region {region}, cannot fetch EBS pricing")
        return None

    pricing = _get_pricing_client()
    volume_family = EBS_VOLUME_TYPE_MAP.get(volume_type, "General Purpose")
    
    try:
        response = pricing.get_products(
            ServiceCode="AmazonEC2",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
                {"Type": "TERM_MATCH", "Field": "volumeType", "Value": volume_family},
            ],
            MaxResults=1,
        )
        price = _extract_price_per_unit(response)
        
        # Only cache successful lookups
        if price is not None:
            _PRICE_CACHE[key] = {"price": price, "timestamp": time.time()}
        
        return price
    except Exception as e:
        logger.error(f"Error fetching EBS price for {volume_type} in {region}: {e}")
        return None


def _get_alb_hourly_price(region):
    """Get ALB hourly price from Pricing API with TTL cache."""
    key = ("ELB", region)
    
    # Check cache
    if key in _PRICE_CACHE:
        cached = _PRICE_CACHE[key]
        if time.time() - cached.get("timestamp", 0) < CACHE_TTL:
            return cached.get("price")
    
    _clean_cache()

    location = _get_location(region)
    if not location:
        logger.warning(f"Unknown region {region}, cannot fetch ELB pricing")
        return None

    pricing = _get_pricing_client()
    
    try:
        response = pricing.get_products(
            ServiceCode="AWSELB",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Load Balancer"},
                {"Type": "TERM_MATCH", "Field": "loadBalancerType", "Value": "Application"},
                {"Type": "TERM_MATCH", "Field": "operation", "Value": "LoadBalancing:Application"},
            ],
            MaxResults=1,
        )
        price = _extract_price_per_unit(response)
        
        # Only cache successful lookups
        if price is not None:
            _PRICE_CACHE[key] = {"price": price, "timestamp": time.time()}
        
        return price
    except Exception as e:
        logger.error(f"Error fetching ELB price for {region}: {e}")
        return None

def estimate_monthly_waste(resources):
    """
    Estimate monthly waste cost for resources.
    Returns new list with cost annotations, does not mutate input.
    """
    pricing_mode = os.environ.get("PRICING_MODE", "static").lower()
    
    # Validate pricing mode
    if pricing_mode not in ["static", "live"]:
        logger.warning(f"Invalid PRICING_MODE '{pricing_mode}', using 'static'")
        pricing_mode = "static"
    
    pricing = _load_pricing()
    report = []
    total = 0
    
    # Deduplicate resources by (type, id)
    seen = set()
    unique_resources = []
    for r in resources:
        key = (r.get("type"), r.get("id"))
        if key not in seen:
            seen.add(key)
            unique_resources.append(r)
        else:
            logger.warning(f"Duplicate resource detected: {key}")
    
    logger.info(f"Estimating costs for {len(unique_resources)} unique resources (pricing_mode={pricing_mode})")

    for r in unique_resources:
        cost = 0
        resource_type = r.get("type", "UNKNOWN")

        if resource_type == "EBS":
            if pricing_mode == "live":
                price_per_gb = _safe_price(
                    _get_ebs_gb_month_price, r.get("volume_type"), r.get("region")
                )
                if price_per_gb is not None:
                    cost = r["size_gb"] * price_per_gb
                else:
                    cost = r["size_gb"] * pricing["EBS"]
            else:
                cost = r["size_gb"] * pricing["EBS"]

        elif resource_type == "EC2":
            if pricing_mode == "live":
                hourly = _safe_price(
                    _get_ec2_hourly_price, r.get("instance_type"), r.get("region")
                )
                if hourly is not None:
                    cost = hourly * HOURS_PER_MONTH
                else:
                    cost = pricing["EC2"].get(r["instance_type"], 50)
            else:
                cost = pricing["EC2"].get(r["instance_type"], 50)

        elif resource_type == "ELB":
            if pricing_mode == "live":
                hourly = _safe_price(_get_alb_hourly_price, r.get("region"))
                if hourly is not None:
                    cost = hourly * HOURS_PER_MONTH
                else:
                    cost = pricing["ELB"]
            else:
                cost = pricing["ELB"]

        elif resource_type in ["RDS", "RDS_CLUSTER", "RDS_INSTANCE"]:
            # RDS pricing is still static (need instance class details for live pricing)
            cost = pricing["RDS"]
        
        else:
            logger.warning(f"Unknown resource type: {resource_type}")
            cost = 0

        # Create new dict with cost annotation (avoid mutation)
        annotated = {**r, "monthly_cost": round(cost, 2)}
        total += cost
        report.append(annotated)

    logger.info(f"Total estimated monthly waste: ${round(total, 2)}")
    return report, round(total, 2)
