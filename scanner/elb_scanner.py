# scanner/elb_scanner.py
import logging
from datetime import datetime, timedelta, timezone

from utils.aws_factory import get_client
from utils.aws_helpers import (
    get_region_from_az,
    safe_get_first,
    chunk_list,
    log_and_skip,
    normalize_tags,
)
from utils.schema import build_resource

logger = logging.getLogger(__name__)

_elbv2_client = None
_elb_client = None
_cloudwatch_client = None


def _get_elbv2_client():
    """Lazy initialization of ELBv2 client (delegates to shared factory)."""
    global _elbv2_client
    if _elbv2_client is None:
        _elbv2_client = get_client("elbv2")
    return _elbv2_client


def _get_elb_client():
    """Lazy initialization of ELB (Classic) client (delegates to shared factory)."""
    global _elb_client
    if _elb_client is None:
        _elb_client = get_client("elb")
    return _elb_client


def _get_cloudwatch_client():
    """Lazy initialization of CloudWatch client (delegates to shared factory)."""
    global _cloudwatch_client
    if _cloudwatch_client is None:
        _cloudwatch_client = get_client("cloudwatch")
    return _cloudwatch_client


def _get_lb_metric_dimension(lb_arn):
    """Extract metric dimension from load balancer ARN.

    Handles app/net/gateway ARNs of form
    arn:...:loadbalancer/app|net|gwy/name/id. Unknown shapes return None
    so the caller can fail-open skip with a log.
    """
    if not lb_arn or not isinstance(lb_arn, str):
        return None
    if "loadbalancer/" in lb_arn:
        suffix = lb_arn.split("loadbalancer/")[-1]
        # Expected: <kind>/<name>/<id>
        parts = suffix.split("/")
        if len(parts) >= 3 and parts[0] in ("app", "net", "gwy"):
            return suffix
        logger.debug(f"Unrecognized LB ARN shape: {lb_arn!r}")
        return None
    tail = lb_arn.split("/")[-1]
    return tail or None


def _get_batch_tags(arns):
    """Batch fetch tags for multiple load balancers (fail-open per chunk)."""
    elbv2 = _get_elbv2_client()
    tag_map = {}

    # Process in chunks of 20 (AWS limit)
    for chunk in chunk_list([a for a in (arns or []) if a], 20):
        try:
            response = elbv2.describe_tags(ResourceArns=chunk)
            for tag_desc in (response.get("TagDescriptions", []) or []):
                arn = tag_desc.get("ResourceArn")
                tags = tag_desc.get("Tags", []) or []
                tag_map[arn] = normalize_tags(tags, source=f"elb:{arn}")
        except Exception as e:
            log_and_skip(f"elb-tags-chunk({len(chunk)})", "describe_tags error", e)
            # Fail-open: empty tags for this chunk
            for arn in chunk:
                tag_map.setdefault(arn, {})

    return tag_map


def _get_batch_tags_classic(names):
    """Batch fetch tags for Classic load balancers by name (fail-open per chunk)."""
    elb = _get_elb_client()
    tag_map = {}

    # Process in chunks of 20 (AWS limit for describe_tags)
    for chunk in chunk_list([n for n in (names or []) if n], 20):
        try:
            response = elb.describe_tags(LoadBalancerNames=chunk)
            for tag_desc in (response.get("TagDescriptions", []) or []):
                name = tag_desc.get("LoadBalancerName")
                tags = tag_desc.get("Tags", []) or []
                tag_map[name] = normalize_tags(tags, source=f"elb-classic:{name}")
        except Exception as e:
            log_and_skip(f"elb-classic-tags-chunk({len(chunk)})", "describe_tags error", e)
            for name in chunk:
                tag_map.setdefault(name, {})

    return tag_map


def _check_alb_nlb_usage(lb, lb_type, start, end):
    """Check if ALB/NLB has received traffic.

    Fail-open (Decision 1): missing datapoints or CW errors -> None (skip),
    never treated as unused. Returns True (unused) / False (used) / None (skip).
    """
    cloudwatch = _get_cloudwatch_client()
    lb_name = (lb or {}).get("LoadBalancerName", "unknown")

    lb_type_norm = (lb_type or "application").lower()
    if lb_type_norm not in ("application", "network"):
        # Gateway and unknown types: wrong metric family would false-positive.
        log_and_skip(lb_name, f"unsupported lb_type {lb_type_norm!r} for metric check")
        return None

    # LBs created within the scan window have no meaningful history -> skip.
    created = (lb or {}).get("CreatedTime")
    try:
        if created is not None and created > start:
            logger.debug(f"LB {lb_name} created after scan window start, skipping")
            return None
    except TypeError:
        # Naive vs aware datetime comparison: fail-open skip.
        logger.debug(f"LB {lb_name} CreatedTime timezone mismatch, skipping")
        return None

    namespace = "AWS/ApplicationELB" if lb_type_norm == "application" else "AWS/NetworkELB"
    metric_name = "RequestCount" if lb_type_norm == "application" else "ProcessedBytes"

    metric_dimension = _get_lb_metric_dimension((lb or {}).get("LoadBalancerArn", ""))
    if not metric_dimension:
        log_and_skip(lb_name, "unparseable LoadBalancerArn")
        return None

    try:
        metrics = cloudwatch.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=[{"Name": "LoadBalancer", "Value": metric_dimension}],
            StartTime=start,
            EndTime=end,
            Period=86400,
            Statistics=["Sum"],
        )
    except Exception as e:
        log_and_skip(lb_name, "get_metric_statistics error", e)
        return None

    datapoints = metrics.get("Datapoints") if isinstance(metrics, dict) else None
    if not datapoints:
        log_and_skip(lb_name, "no CloudWatch datapoints")
        return None

    try:
        total = sum(float(d.get("Sum", 0) or 0) for d in datapoints)
    except (TypeError, ValueError):
        log_and_skip(lb_name, "malformed datapoints")
        return None
    return total == 0


def _check_classic_lb_usage(lb_name, created, start, end):
    """Check if Classic LB has received traffic. Fail-open: None on skip."""
    cloudwatch = _get_cloudwatch_client()

    if created is not None:
        try:
            if created > start:
                logger.debug(f"Classic LB {lb_name} created after scan window start, skipping")
                return None
        except TypeError:
            logger.debug(f"Classic LB {lb_name} CreatedTime timezone mismatch, skipping")
            return None

    try:
        metrics = cloudwatch.get_metric_statistics(
            Namespace="AWS/ELB",
            MetricName="RequestCount",
            Dimensions=[{"Name": "LoadBalancerName", "Value": lb_name}],
            StartTime=start,
            EndTime=end,
            Period=86400,
            Statistics=["Sum"],
        )
    except Exception as e:
        log_and_skip(lb_name, "get_metric_statistics error", e)
        return None

    datapoints = metrics.get("Datapoints") if isinstance(metrics, dict) else None
    if not datapoints:
        log_and_skip(lb_name, "no CloudWatch datapoints")
        return None

    try:
        total = sum(float(d.get("Sum", 0) or 0) for d in datapoints)
    except (TypeError, ValueError):
        log_and_skip(lb_name, "malformed datapoints")
        return None
    return total == 0


def _az_name_from_v2(lb):
    az_list = (lb or {}).get("AvailabilityZones", []) or []
    first = safe_get_first(az_list, {})
    if isinstance(first, dict):
        return first.get("ZoneName", "") or ""
    return ""


def scan_unused_elb():
    """Scan for unused Application, Network, and Classic Load Balancers."""
    elbv2 = _get_elbv2_client()
    elb = _get_elb_client()
    unused = []

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=7)

    logger.info("Starting ELB scan (ALB/NLB/Classic)")

    try:
        # Scan ALB and NLB
        paginator = elbv2.get_paginator("describe_load_balancers")
        lbs = []
        for page in paginator.paginate():
            lbs.extend((page or {}).get("LoadBalancers", []) or [])

        logger.info(f"Found {len(lbs)} ALB/NLB load balancers")

        # Batch fetch tags
        lb_arns = [lb.get("LoadBalancerArn") for lb in lbs if isinstance(lb, dict) and lb.get("LoadBalancerArn")]
        tag_map = _get_batch_tags(lb_arns)

        for lb in lbs:
            if not isinstance(lb, dict) or not lb.get("LoadBalancerName") or not lb.get("LoadBalancerArn"):
                logger.warning("Skipping malformed LB record")
                continue
            lb_type = (lb.get("Type", "application") or "application")

            usage = _check_alb_nlb_usage(lb, lb_type, start, now)
            if usage is True:
                az_name = _az_name_from_v2(lb)
                region = get_region_from_az(az_name)
                unused.append(build_resource(
                    "ELB", lb["LoadBalancerName"], region=region, az=az_name,
                    arn=lb.get("LoadBalancerArn"),
                    reason="unused-zero-traffic",
                    tags=tag_map.get(lb["LoadBalancerArn"], {}),
                    lb_type=str(lb_type).upper(),
                    scheme=lb.get("Scheme", "") or "",
                ))

        # Scan Classic Load Balancers
        classic_paginator = elb.get_paginator("describe_load_balancers")
        classic_lbs = []
        for page in classic_paginator.paginate():
            classic_lbs.extend((page or {}).get("LoadBalancerDescriptions", []) or [])

        logger.info(f"Found {len(classic_lbs)} Classic load balancers")

        classic_names = [lb.get("LoadBalancerName") for lb in classic_lbs if isinstance(lb, dict) and lb.get("LoadBalancerName")]
        classic_tag_map = _get_batch_tags_classic(classic_names)

        for lb in classic_lbs:
            if not isinstance(lb, dict) or not lb.get("LoadBalancerName"):
                continue
            lb_name = lb["LoadBalancerName"]

            usage = _check_classic_lb_usage(lb_name, lb.get("CreatedTime"), start, now)
            if usage is True:
                az_list = lb.get("AvailabilityZones", []) or []
                first_az = safe_get_first(az_list, "") or ""
                if not isinstance(first_az, str):
                    first_az = ""
                unused.append(build_resource(
                    "ELB", lb_name, region=get_region_from_az(first_az), az=first_az,
                    reason="unused-zero-traffic",
                    tags=classic_tag_map.get(lb_name, {}),
                    lb_type="CLASSIC",
                    scheme=lb.get("Scheme", "") or "",
                ))

        logger.info(f"Found {len(unused)} unused load balancers")
    except Exception as e:
        logger.error(f"Error scanning load balancers: {e}", exc_info=True)
        raise

    return unused
