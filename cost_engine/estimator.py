import json
import os
import boto3

HOURS_PER_MONTH = 730
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
    return boto3.client("pricing", region_name="us-east-1")

def _safe_price(fn, *args):
    try:
        return fn(*args)
    except Exception:
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
    key = ("EC2", instance_type, region)
    if key in _PRICE_CACHE:
        return _PRICE_CACHE[key]

    location = _get_location(region)
    if not location:
        return None

    pricing = _get_pricing_client()
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
    _PRICE_CACHE[key] = price
    return price

def _get_ebs_gb_month_price(volume_type, region):
    key = ("EBS", volume_type, region)
    if key in _PRICE_CACHE:
        return _PRICE_CACHE[key]

    location = _get_location(region)
    if not location:
        return None

    pricing = _get_pricing_client()
    volume_family = EBS_VOLUME_TYPE_MAP.get(volume_type, "General Purpose")
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
    _PRICE_CACHE[key] = price
    return price

def _get_alb_hourly_price(region):
    key = ("ELB", region)
    if key in _PRICE_CACHE:
        return _PRICE_CACHE[key]

    location = _get_location(region)
    if not location:
        return None

    pricing = _get_pricing_client()
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
    _PRICE_CACHE[key] = price
    return price

def estimate_monthly_waste(resources):
    pricing_mode = os.environ.get("PRICING_MODE", "static").lower()
    pricing = _load_pricing()
    report = []
    total = 0

    for r in resources:
        cost = 0

        if r["type"] == "EBS":
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

        elif r["type"] == "EC2":
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

        elif r["type"] == "ELB":
            if pricing_mode == "live":
                hourly = _safe_price(_get_alb_hourly_price, r.get("region"))
                if hourly is not None:
                    cost = hourly * HOURS_PER_MONTH
                else:
                    cost = pricing["ELB"]
            else:
                cost = pricing["ELB"]

        elif r["type"] == "RDS":
            cost = pricing["RDS"]

        r["monthly_cost"] = round(cost, 2)
        total += cost
        report.append(r)

    return report, round(total, 2)
