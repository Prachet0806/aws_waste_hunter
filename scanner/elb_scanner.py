# scanner/elb_scanner.py
import boto3
import os
import logging
from datetime import datetime, timedelta, timezone
from utils.aws_helpers import BOTO3_CONFIG, get_region_from_az, safe_get_first, chunk_list

logger = logging.getLogger(__name__)

_elbv2_client = None
_elb_client = None
_cloudwatch_client = None


def _get_elbv2_client():
    """Lazy initialization of ELBv2 client."""
    global _elbv2_client
    if _elbv2_client is None:
        region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION"))
        _elbv2_client = boto3.client("elbv2", region_name=region, config=BOTO3_CONFIG)
    return _elbv2_client


def _get_elb_client():
    """Lazy initialization of ELB (Classic) client."""
    global _elb_client
    if _elb_client is None:
        region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION"))
        _elb_client = boto3.client("elb", region_name=region, config=BOTO3_CONFIG)
    return _elb_client


def _get_cloudwatch_client():
    """Lazy initialization of CloudWatch client."""
    global _cloudwatch_client
    if _cloudwatch_client is None:
        region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION"))
        _cloudwatch_client = boto3.client("cloudwatch", region_name=region, config=BOTO3_CONFIG)
    return _cloudwatch_client


def _get_lb_metric_dimension(lb_arn):
    """Extract metric dimension from load balancer ARN."""
    if "loadbalancer/" in lb_arn:
        return lb_arn.split("loadbalancer/")[-1]
    return lb_arn.split("/")[-1]


def _get_batch_tags(arns):
    """Batch fetch tags for multiple load balancers."""
    elbv2 = _get_elbv2_client()
    tag_map = {}
    
    # Process in chunks of 20 (AWS limit)
    for chunk in chunk_list(arns, 20):
        try:
            response = elbv2.describe_tags(ResourceArns=chunk)
            for tag_desc in response.get("TagDescriptions", []):
                arn = tag_desc.get("ResourceArn")
                tags = tag_desc.get("Tags", [])
                tag_map[arn] = {t["Key"]: t["Value"] for t in tags}
        except Exception as e:
            logger.warning(f"Error fetching tags for chunk: {e}")
            # Return empty tags for this chunk
            for arn in chunk:
                tag_map[arn] = {}
    
    return tag_map


def _check_alb_nlb_usage(lb, lb_type, start, end):
    """Check if ALB/NLB has received traffic."""
    cloudwatch = _get_cloudwatch_client()
    
    namespace = "AWS/ApplicationELB" if lb_type == "application" else "AWS/NetworkELB"
    metric_name = "RequestCount" if lb_type == "application" else "ProcessedBytes"
    
    metric_dimension = _get_lb_metric_dimension(lb["LoadBalancerArn"])
    
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
        
        # Check for missing datapoints explicitly
        if not metrics.get("Datapoints"):
            logger.debug(f"No datapoints for {lb['LoadBalancerName']}, treating as unused")
            return True
        
        total = sum(d["Sum"] for d in metrics["Datapoints"])
        return total == 0
    except Exception as e:
        logger.warning(f"Error checking metrics for {lb['LoadBalancerName']}: {e}")
        return False


def _check_classic_lb_usage(lb_name, start, end):
    """Check if Classic LB has received traffic."""
    cloudwatch = _get_cloudwatch_client()
    
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
        
        if not metrics.get("Datapoints"):
            logger.debug(f"No datapoints for Classic LB {lb_name}, treating as unused")
            return True
        
        total = sum(d["Sum"] for d in metrics["Datapoints"])
        return total == 0
    except Exception as e:
        logger.warning(f"Error checking metrics for Classic LB {lb_name}: {e}")
        return False


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
            lbs.extend(page.get("LoadBalancers", []))

        logger.info(f"Found {len(lbs)} ALB/NLB load balancers")

        # Batch fetch tags
        lb_arns = [lb["LoadBalancerArn"] for lb in lbs]
        tag_map = _get_batch_tags(lb_arns)

        for lb in lbs:
            lb_type = lb.get("Type", "application")
            
            if _check_alb_nlb_usage(lb, lb_type, start, now):
                az_list = lb.get("AvailabilityZones", [])
                first_az = safe_get_first(az_list, {})
                az_name = first_az.get("ZoneName", "") if isinstance(first_az, dict) else ""
                
                unused.append({
                    "type": "ELB",
                    "id": lb["LoadBalancerName"],
                    "lb_type": lb_type.upper(),
                    "scheme": lb.get("Scheme", ""),
                    "az": az_name,
                    "region": get_region_from_az(az_name),
                    "tags": tag_map.get(lb["LoadBalancerArn"], {})
                })

        # Scan Classic Load Balancers
        classic_paginator = elb.get_paginator("describe_load_balancers")
        classic_lbs = []
        for page in classic_paginator.paginate():
            classic_lbs.extend(page.get("LoadBalancerDescriptions", []))

        logger.info(f"Found {len(classic_lbs)} Classic load balancers")

        for lb in classic_lbs:
            lb_name = lb["LoadBalancerName"]
            
            if _check_classic_lb_usage(lb_name, start, now):
                az_list = lb.get("AvailabilityZones", [])
                first_az = safe_get_first(az_list, "")
                
                # Classic LBs don't support describe_tags via ARN, skip tags
                unused.append({
                    "type": "ELB",
                    "id": lb_name,
                    "lb_type": "CLASSIC",
                    "scheme": lb.get("Scheme", ""),
                    "az": first_az,
                    "region": get_region_from_az(first_az),
                    "tags": {}
                })

        logger.info(f"Found {len(unused)} unused load balancers")
    except Exception as e:
        logger.error(f"Error scanning load balancers: {e}", exc_info=True)
        raise

    return unused
